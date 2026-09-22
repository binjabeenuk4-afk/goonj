"""
Goonj backend — free unlimited TTS powered by edge-tts (no API key, no cost).

Endpoints (all under /api):
  GET  /api/voices?search=&lang=      list voices, cached 6h, filterable
  POST /api/tts                       {voice_id, text, rate, pitch, emotions} -> job
  POST /api/podcast                   {mode, voice1, voice2, script, ...} -> job
  GET  /api/job/{job_id}              job status / progress / audio_url
  GET  /api/preview/{voice_id}        short sample mp3, cached on disk
Static:
  /outputs/...   generated mp3s
  /              frontend (index.html)
"""

import asyncio
import io
import logging
import os
import re
import time
import uuid
from pathlib import Path

import edge_tts
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# --- Server proxy TLS fix -------------------------------------------------
# This server reaches the internet through an egress proxy whose CA is NOT in
# certifi's bundle. edge_tts hardcodes certifi, so we patch its SSL contexts
# to also trust the proxy CA (from SSL_CERT_FILE when present).
try:
    import certifi
    import os as _os
    import ssl as _ssl

    _proxy_ca = _os.environ.get("SSL_CERT_FILE")
    if _proxy_ca and _os.path.exists(_proxy_ca):
        _ctx = _ssl.create_default_context(cafile=certifi.where())
        _ctx.load_verify_locations(cafile=_proxy_ca)
        import edge_tts.communicate as _comm
        import edge_tts.voices as _voices

        _comm._SSL_CTX = _ctx
        _voices._SSL_CTX = _ctx
        log_extra = f"patched edge_tts SSL context with {_proxy_ca}"
    else:
        log_extra = "no proxy CA found, using default SSL context"
except Exception as _e:  # noqa: BLE001
    log_extra = f"SSL patch skipped: {_e}"
print(f"[goonj] {log_extra}")

from backend import raw_ws_tts  # raw-socket edge-tts client; see module docstring

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("goonj")

BASE = Path(__file__).resolve().parent
OUTPUTS = BASE / "outputs"
PREVIEWS = OUTPUTS / "previews"
FRONTEND = BASE.parent / "frontend"
OUTPUTS.mkdir(exist_ok=True)
PREVIEWS.mkdir(exist_ok=True)

MAX_CHARS = 60000  # ~1 hour of audio; processed as an async job
CHUNK_SIZE = 2000
CHUNK_RETRIES = 3
CHUNK_BACKOFF = (2, 5)  # seconds between retries
MAX_JOBS = 100  # in-memory job history cap
VOICE_REFRESH_SECS = 6 * 3600
PREVIEW_TEXT = (
    "Assalam o Alaikum! Yeh meri awaz ka preview hai. "
    "Hello, this is a voice preview."
)

VOICES: list = []
VOICES_UPDATED = 0.0
_voices_lock = asyncio.Lock()

RATE_RE = re.compile(r"^[+-]\d{1,3}%$")
PITCH_RE = re.compile(r"^[+-]\d{1,3}Hz$")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?\u06d4\u061f\u061b\n])\s+")
_POD_LINE = re.compile(r"^(host|guest|1|2)\s*:\s*(.+)$", re.IGNORECASE)


def _make_silence_mp3(ms: int = 600) -> bytes:
    """Generate a short silent mp3 (24kHz/48k mono) via ffmpeg for gaps."""
    try:
        import subprocess
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
             "-t", str(ms / 1000), "-c:a", "libmp3lame", "-b:a", "48k",
             "-id3v2_version", "0", "-f", "mp3", "-"],
            capture_output=True, timeout=30)
        data = p.stdout or b""
        # MPEG frame sync: 0xFF followed by 3 more sync bits (0xE0 mask)
        if p.returncode == 0 and len(data) > 2 and data[0] == 0xFF \
                and data[1] & 0xE0 == 0xE0:
            return data
        log.warning("silence mp3 generation failed (rc=%s, %d bytes)",
                    p.returncode, len(data))
    except Exception as e:  # noqa: BLE001
        log.warning("silence mp3 generation failed: %s", e)
    return b""


SILENCE_MP3 = _make_silence_mp3()  # 600ms gap between podcast speakers
print(f"[goonj] silence gap mp3: {len(SILENCE_MP3)} bytes")


async def load_voices(force: bool = False) -> list:
    """Fetch the edge-tts voice list, cached in memory for 6 hours."""
    global VOICES, VOICES_UPDATED
    now = time.time()
    if VOICES and not force and now - VOICES_UPDATED < VOICE_REFRESH_SECS:
        return VOICES
    async with _voices_lock:
        now = time.time()
        if VOICES and not force and now - VOICES_UPDATED < VOICE_REFRESH_SECS:
            return VOICES
        try:
            raw = await edge_tts.list_voices(proxy=_get_proxy())
        except Exception as e:  # noqa: BLE001 - surface as clean error
            log.warning("list_voices failed: %s", e)
            if VOICES:
                return VOICES
            raise HTTPException(
                status_code=502,
                detail="Could not reach the voice service. Please try again.",
            )
        VOICES = [
            {
                "id": v["ShortName"],
                "name": v["FriendlyName"],
                "gender": v.get("Gender"),
                "locale": v.get("Locale"),
            }
            for v in raw
        ]
        VOICES_UPDATED = time.time()
        log.info("Loaded %d voices", len(VOICES))
    return VOICES


def _voice_ids() -> set:
    return {v["id"] for v in VOICES}


def chunk_text(text: str, size: int = CHUNK_SIZE) -> list:
    """Split text into chunks <= size chars on sentence boundaries."""
    sentences = [s for s in _SENTENCE_SPLIT.split(text.strip()) if s.strip()]
    if not sentences:
        return [text.strip()] if text.strip() else []
    chunks, current = [], ""
    for s in sentences:
        if len(current) + len(s) + 1 <= size:
            current = (current + " " + s).strip()
        else:
            if current:
                chunks.append(current)
            # a single sentence longer than size: hard-split it
            while len(s) > size:
                chunks.append(s[:size])
                s = s[size:]
            current = s
    if current:
        chunks.append(current)
    return chunks


def _get_proxy() -> str | None:
    """Egress proxy URL from env (needed: edge_tts websocket ignores trust_env)."""
    return os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")


async def synthesize(text: str, voice_id: str, rate: str, pitch: str,
                     emotions: bool = True) -> bytes:
    """Generate mp3 bytes for text, chunking long input.

    Uses the raw-socket edge-tts client (backend/raw_ws_tts.py) because the
    egress proxy breaks aiohttp's websocket handshake. Each chunk runs in a
    thread so the event loop stays responsive. When emotions is on, inline
    [markers] and auto-pauses become per-segment prosody and silence gaps
    (the endpoint rejects raw SSML elements, so this is segment-based).
    """
    items = _build_tts_items(text, voice_id, rate, pitch, emotions)
    audio = io.BytesIO()
    for it in items:
        if it[0] == "gap":
            _write_gap(audio, it[1])
            continue
        _, chunk, vid, r, p = it
        try:
            data = await asyncio.to_thread(
                raw_ws_tts.synthesize_sync, chunk, vid, r, p
            )
            audio.write(data)
        except (RuntimeError, ConnectionError) as e:
            raise HTTPException(
                status_code=502,
                detail="The voice service returned no audio for this text.",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("TTS chunk failed: %s", e)
            raise HTTPException(
                status_code=502,
                detail="Voice generation failed. Please try again.",
            )
    data = audio.getvalue()
    if not data:
        raise HTTPException(
            status_code=502,
            detail="The voice service returned no audio for this text.",
        )
    return data


# --------------------------------------------------------------------------
# Async long-form jobs: POST /api/tts returns a job_id immediately; a
# background thread renders chunks sequentially (with retry) and joins them
# into one mp3. Progress is polled via GET /api/job/{job_id}.
# --------------------------------------------------------------------------
import threading as _threading

_jobs: dict = {}
_jobs_lock = _threading.Lock()


def _combine_rate(base: str, delta: int) -> str:
    """Combine a user rate like "+20%" with a relative delta percent."""
    b = int(base[1:-1])
    bv = b if base[0] == "+" else -b
    c = max(-100, min(100, round((1 + bv / 100) * (1 + delta / 100) * 100 - 100)))
    return f"{c:+d}%"


def _shift_pitch(base: str, delta: int) -> str:
    """Shift a user pitch like "+0Hz" by delta Hz, clamped to +-100."""
    b = int(base[1:-2])
    bv = b if base[0] == "+" else -b
    c = max(-100, min(100, bv + delta))
    return f"{c:+d}Hz"


def _prosody_adjust(rate: str, pitch: str, kind: str | None) -> tuple:
    """Apply an emotion kind's rate/pitch deltas on top of user settings."""
    if not kind or kind not in raw_ws_tts.EMOTION_ADJUST:
        return rate, pitch
    adj = raw_ws_tts.EMOTION_ADJUST[kind]
    return (_combine_rate(rate, adj["rate_delta"]),
            _shift_pitch(pitch, adj["pitch_delta"]))


def _write_gap(buf, ms: int) -> None:
    """Write ms milliseconds of silence (in 600ms units) into buf."""
    if not SILENCE_MP3:
        return
    for _ in range(max(1, round(ms / 600))):
        buf.write(SILENCE_MP3)


def _silence_for_text(text: str) -> bytes:
    """Render silence proportional to text length (fallback for unvoicable chunks).

    Some voice models return zero audio for certain inputs (verified: ur-PK
    voices on specific roman-Urdu phrases). Rather than failing a whole job,
    we insert matching silence and flag a warning on the job.
    """
    ms = min(10000, max(500, int(len(text) * 60000 / 700)))
    try:
        import subprocess
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
             "-t", str(ms / 1000), "-c:a", "libmp3lame", "-b:a", "48k",
             "-id3v2_version", "0", "-f", "mp3", "-"],
            capture_output=True, timeout=30)
        if p.returncode == 0 and p.stdout and p.stdout[0] == 0xFF:
            return p.stdout
    except Exception as e:  # noqa: BLE001
        log.warning("fallback silence generation failed: %s", e)
    # last resort: 600ms units
    buf = io.BytesIO()
    _write_gap(buf, ms)
    return buf.getvalue()


# Work items: ("say", text, voice_id, rate, pitch) or ("gap", ms).
def _ops_to_items(ops: list, voice_id: str, rate: str, pitch: str) -> list:
    items = []
    for op in ops:
        if op[0] == "pause":
            items.append(("gap", op[1]))
        else:
            kind, seg = op[1], op[2]
            r, p = _prosody_adjust(rate, pitch, kind)
            for ch in chunk_text(seg):
                items.append(("say", ch, voice_id, r, p))
    return items


def _build_tts_items(text: str, voice_id: str, rate: str, pitch: str,
                     emotions: bool) -> list:
    if emotions:
        ops = raw_ws_tts.parse_emotion_script(text, auto_emotions=True)
    else:
        ops = [("say", None, text)]
    return _ops_to_items(ops, voice_id, rate, pitch)


def _build_podcast_items(segments: list, rate: str, pitch: str,
                         emotions: bool) -> list:
    items = []
    for idx, (voice_id, line) in enumerate(segments):
        if idx:
            items.append(("gap", 600))  # silence between speakers/lines
        if emotions:
            ops = raw_ws_tts.parse_emotion_script(line, auto_emotions=True)
        else:
            ops = [("say", None, line)]
        items.extend(_ops_to_items(ops, voice_id, rate, pitch))
    return items


def _say_count(items: list) -> int:
    return sum(1 for it in items if it[0] == "say")


def _render_chunk(chunk: str, voice_id: str, rate: str, pitch: str) -> bytes:
    """Render one chunk with retry. Raises RuntimeError if all attempts fail."""
    last_err = None
    for attempt in range(CHUNK_RETRIES):
        try:
            return raw_ws_tts.synthesize_sync(chunk, voice_id, rate, pitch)
        except Exception as e:  # noqa: BLE001 - retry then fail clean
            last_err = e
            log.warning("chunk render attempt %d failed: %s", attempt + 1, e)
            if attempt < CHUNK_RETRIES - 1:
                time.sleep(CHUNK_BACKOFF[min(attempt, len(CHUNK_BACKOFF) - 1)])
    raise RuntimeError(f"chunk failed after {CHUNK_RETRIES} attempts: {last_err}")


def _job_new(voice_id: str, text: str, rate: str, pitch: str,
             total_say: int) -> dict:
    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "status": "queued",  # queued | working | done | error
        "chunks_done": 0,
        "chunks_total": total_say,
        "progress_pct": 0,
        "audio_url": None,
        "error": None,
        "characters": len(text),
        "voice_id": voice_id,
        "warnings": [],
    }
    with _jobs_lock:
        _jobs[job_id] = job
        while len(_jobs) > MAX_JOBS:  # evict oldest
            oldest = next(iter(_jobs))
            _jobs.pop(oldest, None)
    return job


def _job_set(job_id: str, **fields) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            job.update(fields)


def _save_job_audio(job_id: str, data: bytes) -> bool:
    """Write finished mp3 to disk and mark the job done. False on empty data."""
    if not data:
        _job_set(job_id, status="error",
                 error="The voice service returned no audio for this text.")
        return False
    fname = f"job-{job_id}.mp3"
    (OUTPUTS / fname).write_bytes(data)
    _job_set(job_id, status="done", progress_pct=100,
             audio_url=f"/outputs/{fname}")
    return True


def _run_items_job(job_id: str, items: list) -> None:
    """Background worker: render every work item sequentially into one mp3."""
    _job_set(job_id, status="working")
    total = _say_count(items)
    out = io.BytesIO()
    done = 0
    try:
        for it in items:
            if it[0] == "gap":
                _write_gap(out, it[1])
                continue
            _, chunk, voice_id, rate, pitch = it
            try:
                out.write(_render_chunk(chunk, voice_id, rate, pitch))
            except RuntimeError as e:
                # Voice-model quirk (zero-audio chunk): keep the job alive
                # with proportional silence and flag it for the user.
                log.warning("chunk %d unvoicable, inserting silence: %s",
                            done + 1, e)
                out.write(_silence_for_text(chunk))
                with _jobs_lock:
                    job = _jobs.get(job_id)
                    if job is not None:
                        job["warnings"].append(
                            f"Part {done + 1} could not be voiced "
                            f"({voice_id}); silence was inserted instead."
                        )
            done += 1
            _job_set(job_id, chunks_done=done,
                     progress_pct=round(done / total * 100) if total else 100)
        _save_job_audio(job_id, out.getvalue())
    except Exception as e:  # noqa: BLE001 - never leak a stack trace
        log.warning("job %s failed: %s", job_id, e)
        _job_set(job_id, status="error",
                 error="Voice generation failed. Please try again.")


def _sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:80]


async def _check_voice_params(voice_id: str, rate: str, pitch: str) -> None:
    """Validate voice_id/rate/pitch. Raises HTTPException(400) on bad input."""
    await load_voices()
    if voice_id not in _voice_ids():
        raise HTTPException(status_code=400, detail="Unknown voice_id.")
    if not RATE_RE.match(rate or ""):
        raise HTTPException(status_code=400, detail="Invalid rate. Use e.g. +0%, -20%, +50%.")
    if not PITCH_RE.match(pitch or ""):
        raise HTTPException(status_code=400, detail="Invalid pitch. Use e.g. +0Hz, -10Hz.")
    rate_n = int(rate[1:-1])
    pitch_n = int(pitch[1:-2])
    if not (-100 <= rate_n <= 100) or not (-100 <= pitch_n <= 100):
        raise HTTPException(status_code=400, detail="rate and pitch must be within -100..+100.")


class TTSRequest(BaseModel):
    voice_id: str
    text: str
    rate: str = "+0%"
    pitch: str = "+0Hz"
    emotions: bool = True  # inline [markers] + auto-pauses -> SSML


app = FastAPI(title="Goonj", version="1.0.0")

# --- Public API hardening ---------------------------------------------------
# The static frontend on GitHub Pages calls this API cross-origin, so CORS
# must allow that origin. Light in-memory rate limits keep the free service
# usable: max 4 concurrent renders, max 30 new jobs per IP per hour.
PUBLIC_ORIGINS = [
    "https://binjabeenuk4-afk.github.io",
    "http://localhost:8765",
    "http://127.0.0.1:8765",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=PUBLIC_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
    max_age=86400,
)

_MAX_CONCURRENT_JOBS = 4
_MAX_JOBS_PER_IP_PER_HOUR = 30
_job_creations: dict = {}  # ip -> [timestamps]
_job_creations_lock = _threading.Lock()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit_check(request: Request) -> None:
    ip = _client_ip(request)
    now = time.time()
    with _job_creations_lock:
        stamps = [t for t in _job_creations.get(ip, []) if now - t < 3600]
        if len(stamps) >= _MAX_JOBS_PER_IP_PER_HOUR:
            raise HTTPException(
                status_code=429,
                detail="Too many requests — please wait a little and try again.",
            )
        stamps.append(now)
        _job_creations[ip] = stamps
    with _jobs_lock:
        active = sum(1 for j in _jobs.values()
                     if j["status"] in ("queued", "working"))
    if active >= _MAX_CONCURRENT_JOBS:
        raise HTTPException(
            status_code=429,
            detail="The voice studio is busy — please try again in a minute.",
        )


# --- Job audio cleanup: one temp file per job, deleted after 12h -----------
_JOB_TTL_SECS = 12 * 3600


def _sweep_old_jobs() -> None:
    cutoff = time.time() - _JOB_TTL_SECS
    try:
        for f in OUTPUTS.glob("job-*.mp3"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


def _cleanup_loop() -> None:
    while True:
        time.sleep(1800)
        _sweep_old_jobs()


@app.on_event("startup")
async def _preload() -> None:
    try:
        await load_voices()
    except HTTPException:
        log.warning("Voice preload failed; will retry on first request.")
    _sweep_old_jobs()
    _threading.Thread(target=_cleanup_loop, daemon=True).start()


@app.get("/api/health")
async def health():
    return {"ok": True, "voices_cached": len(VOICES)}


@app.get("/api/voices")
async def voices(search: str = "", lang: str = ""):
    items = await load_voices()
    search = search.strip().lower()
    lang = lang.strip().lower()
    out = []
    for v in items:
        if search and search not in (
            v["name"].lower() + " " + v["id"].lower() + " " + (v.get("locale") or "").lower()
        ):
            continue
        if lang and not (v.get("locale") or "").lower().startswith(lang):
            continue
        out.append(v)
    return {"count": len(out), "voices": out}


@app.post("/api/tts")
async def tts(req: TTSRequest, request: Request):
    """Start an async generation job. Returns {job_id} immediately.

    Accepts up to 60000 chars (~1 hour of audio). Chunks render sequentially
    in a background thread; poll GET /api/job/{job_id} for progress.
    """
    _rate_limit_check(request)
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text is empty.")
    if len(text) > MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Text too long: {len(text)} characters (max {MAX_CHARS}).",
        )
    await _check_voice_params(req.voice_id, req.rate, req.pitch)

    items = _build_tts_items(text, req.voice_id, req.rate, req.pitch,
                           req.emotions)
    job = _job_new(req.voice_id, text, req.rate, req.pitch, _say_count(items))
    worker = _threading.Thread(
        target=_run_items_job,
        args=(job["job_id"], items),
        daemon=True,
    )
    worker.start()
    return {"job_id": job["job_id"]}


class PodcastRequest(BaseModel):
    mode: str = "single"  # single | dual
    voice1: str           # the voice (single) or Host (dual)
    voice2: str = ""      # Guest voice (dual mode only)
    script: str
    rate: str = "+0%"
    pitch: str = "+0Hz"
    emotions: bool = True


@app.post("/api/podcast")
async def podcast(req: PodcastRequest, request: Request):
    """Start an async podcast job.

    Single mode: each non-empty line is spoken by voice1.
    Dual mode: each non-empty line must start with "Host:"/"Guest:"
    (also accepts "1:"/"2:"), spoken by voice1/voice2. Lines are joined
    with 600ms silence gaps. Returns {job_id}; poll GET /api/job/{job_id}.
    """
    _rate_limit_check(request)
    mode = (req.mode or "single").lower()
    if mode not in ("single", "dual"):
        raise HTTPException(status_code=400, detail="mode must be 'single' or 'dual'.")
    await _check_voice_params(req.voice1, req.rate, req.pitch)
    if mode == "dual":
        if not req.voice2:
            raise HTTPException(status_code=400,
                                detail="Pick a Guest voice for dual-speaker mode.")
        await load_voices()
        if req.voice2 not in _voice_ids():
            raise HTTPException(status_code=400, detail="Unknown Guest voice.")
    script = (req.script or "").strip()
    if not script:
        raise HTTPException(status_code=400, detail="Script is empty.")
    if len(script) > MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Script too long: {len(script)} characters (max {MAX_CHARS}).",
        )
    lines = [l.strip() for l in script.splitlines() if l.strip()]
    if not lines:
        raise HTTPException(status_code=400, detail="Script is empty.")
    segments: list = []  # (voice_id, text)
    if mode == "single":
        segments = [(req.voice1, l) for l in lines]
    else:
        for n, line in enumerate(lines, 1):
            m = _POD_LINE.match(line)
            if not m:
                raise HTTPException(
                    status_code=400,
                    detail=f"Line {n} needs a speaker — start it with 'Host:' or 'Guest:'.",
                )
            who, said = m.group(1).lower(), m.group(2).strip()
            if not said:
                raise HTTPException(
                    status_code=400,
                    detail=f"Line {n} has no text after the speaker name.",
                )
            segments.append((req.voice1 if who in ("host", "1") else req.voice2, said))
    items = _build_podcast_items(segments, req.rate, req.pitch, req.emotions)
    job = _job_new(req.voice1, script, req.rate, req.pitch, _say_count(items))
    worker = _threading.Thread(
        target=_run_items_job,
        args=(job["job_id"], items),
        daemon=True,
    )
    worker.start()
    return {"job_id": job["job_id"]}


@app.get("/api/job/{job_id}")
async def job_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job_id.")
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "progress_pct": job["progress_pct"],
        "chunks_done": job["chunks_done"],
        "chunks_total": job["chunks_total"],
        "characters": job["characters"],
        "audio_url": job["audio_url"],
        "error": job["error"],
        "warnings": job.get("warnings", []),
    }


@app.get("/api/preview/{voice_id}")
async def preview(voice_id: str):
    voices = await load_voices()
    if voice_id not in _voice_ids():
        raise HTTPException(status_code=400, detail="Unknown voice_id.")
    path = PREVIEWS / f"{_sanitize(voice_id)}.mp3"
    if not path.exists():
        data = await synthesize(PREVIEW_TEXT, voice_id, "+0%", "+0Hz")
        path.write_bytes(data)
    return FileResponse(path, media_type="audio/mpeg", filename=path.name)


@app.exception_handler(HTTPException)
async def _http_exc(_request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


app.mount("/outputs", StaticFiles(directory=str(OUTPUTS)), name="outputs")
# Local dev serves the frontend too; on PaaS (Koyeb etc.) only the API is
# deployed, so mount the static frontend only when the directory exists.
if FRONTEND.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True),
              name="frontend")
else:
    @app.get("/")
    async def _root():
        return {"ok": True, "service": "goonj-api",
                "frontend": "https://binjabeenuk4-afk.github.io/goonj/"}


if __name__ == "__main__":
    # Local or Hugging Face Docker Space: PORT env wins, default 8765.
    import uvicorn

    _port = int(os.environ.get("PORT", "8765"))
    uvicorn.run("backend.main:app", host="0.0.0.0", port=_port)
