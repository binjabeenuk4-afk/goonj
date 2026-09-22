"""
Voice cloning for Goonj — OpenVoice v2 tone-color conversion (MIT licensed,
myshell-ai/OpenVoice code vendored under backend/openvoice/).

Architecture: OpenVoice does voice CONVERSION, not TTS. The pipeline is:
  1. The user uploads a 6-30s voice sample ONCE. We extract a speaker
     embedding ("tone-color vector") and store it as clone_<id>.
  2. At synthesize time the normal edge-tts pipeline renders the text with a
     base neural voice (Urdu/Hindi/English all work — the base voice handles
     pronunciation), then the converter re-timbers that audio into the
     cloned voice. Conversion is content-agnostic, so it works for Urdu
     even though OpenVoice never saw Urdu in training.

Models live in backend/models/ (baked into the Docker image at build time,
so no runtime download surprise). The converter runs at the sample rate in
its config (22050 Hz for the v2 checkpoint) — always read from the model,
never hardcoded. Cloned-voice embeddings live in backend/cloned_voices/.

NOTE on persistence: this host's filesystem is ephemeral — files written to
backend/cloned_voices/ at RUNTIME (user-created voices) vanish on
redeploy/restart. Baked-in models survive because they are in the image.
A future version can back cloned voices with object storage.

Quality note (from OpenVoice docs/issues): young/mid-age FEMALE base
voices convert most recognizably; male base voices can come out
unrecognizable. main.py therefore renders clone jobs with a female base
voice for the detected script language.
"""

import hashlib
import io
import json
import logging
import os
import subprocess
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

import numpy as np
import soundfile
import torch

from backend.openvoice import utils
from backend.openvoice.mel_processing import spectrogram_torch
from backend.openvoice.models import SynthesizerTrn

log = logging.getLogger("goonj.clone")

BASE = Path(__file__).resolve().parent
MODELS_DIR = BASE / "models"
CONVERTER_DIR = MODELS_DIR / "converter"
CONFIG_PATH = CONVERTER_DIR / "config.json"
CKPT_PATH = CONVERTER_DIR / "checkpoint.pth"

CLONES_DIR = BASE / "cloned_voices"
META_PATH = CLONES_DIR / "voices.json"

# Official HuggingFace mirror of the OpenVoice v2 converter checkpoint
# (the old S3 zip link now 404s).
HF_CONVERTER_BASE = "https://huggingface.co/myshell-ai/OpenVoiceV2/resolve/main/converter"

MIN_SAMPLE_SECS = 6.0
MAX_SAMPLE_SECS = 30.0
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


def _target_sr() -> int:
    """Sample rate the converter works at (22050 for the v2 checkpoint).

    Read from the model config, never hardcoded — a different checkpoint
    could use a different rate and the spectrogram math must match the
    actual audio.
    """
    return int(_get_converter()["hps"].data.sampling_rate)


# --------------------------------------------------------------------------
# Model download (also called at Docker build time)
# --------------------------------------------------------------------------
def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log.info("downloading %s -> %s", url, dest)
    req = urllib.request.Request(url, headers={"User-Agent": "goonj/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            t0 = time.time()
            while True:
                chunk = r.read(4 * 1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total and time.time() - t0 > 5:
                    log.info("  ... %d/%d MB", done // 1048576, total // 1048576)
                    t0 = time.time()
    except Exception as e:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise RuntimeError(f"model download failed ({url}): {e}") from e
    tmp.replace(dest)


def ensure_models() -> Path:
    """Make sure the v2 converter checkpoint is on disk; download if not.

    Raises RuntimeError with a clear message on failure (so a Docker build
    fails loudly instead of shipping a broken image).
    """
    if CONFIG_PATH.exists() and CKPT_PATH.exists():
        return CONVERTER_DIR
    log.info("OpenVoice v2 converter not found locally; downloading (~125 MB)...")
    try:
        _download(f"{HF_CONVERTER_BASE}/config.json", CONFIG_PATH)
        _download(f"{HF_CONVERTER_BASE}/checkpoint.pth", CKPT_PATH)
    except Exception as e:
        raise RuntimeError(
            "Could not download the OpenVoice v2 converter checkpoint. "
            "Check network access to huggingface.co. "
            f"Details: {e}"
        ) from e
    if not (CONFIG_PATH.exists() and CKPT_PATH.exists()):
        raise RuntimeError("Converter download incomplete: files missing after download.")
    log.info("converter models ready at %s", CONVERTER_DIR)
    return CONVERTER_DIR


# --------------------------------------------------------------------------
# Converter (lazy singleton — normal TTS startup must stay light)
# --------------------------------------------------------------------------
_converter = None
_converter_lock = None


def _get_converter():
    """Load the tone-color converter on first use. CPU-only."""
    global _converter, _converter_lock
    if _converter is not None:
        return _converter
    import threading

    if _converter_lock is None:
        _converter_lock = threading.Lock()
    with _converter_lock:
        if _converter is not None:
            return _converter
        ensure_models()
        hps = utils.get_hparams_from_file(str(CONFIG_PATH))
        model = SynthesizerTrn(
            len(getattr(hps, "symbols", [])),
            hps.data.filter_length // 2 + 1,
            n_speakers=hps.data.n_speakers,
            **hps.model,
        )
        model.eval()
        ckpt = torch.load(str(CKPT_PATH), map_location=torch.device("cpu"),
                          weights_only=True)
        model.load_state_dict(ckpt["model"], strict=False)
        log.info("OpenVoice v2 converter loaded (sampling_rate=%s)",
                 hps.data.sampling_rate)
        _converter = {"model": model, "hps": hps}
        return _converter


def _sample_rate() -> int:
    return int(_get_converter()["hps"].data.sampling_rate)


# --------------------------------------------------------------------------
# Audio helpers
# --------------------------------------------------------------------------
def probe_duration_secs(path: str | Path) -> float:
    """Audio duration in seconds via ffprobe. Raises RuntimeError on failure."""
    p = subprocess.run(
        ["ffprobe", "-hide_banner", "-loglevel", "error",
         "-show_entries", "format=duration", "-of",
         "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=30)
    if p.returncode != 0 or not p.stdout.strip():
        raise RuntimeError(f"could not probe audio duration: {p.stderr.strip()}")
    return float(p.stdout.strip())


def to_target_wav(src_path: str | Path, dst_path: str | Path) -> Path:
    """Convert any audio file to mono wav at the converter's sample rate."""
    sr = _target_sr()
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(src_path), "-ac", "1", "-ar", str(sr),
         "-c:a", "pcm_s16le", str(dst_path)],
        capture_output=True, timeout=120)
    if p.returncode != 0 or not Path(dst_path).exists():
        raise RuntimeError(f"audio conversion to {sr}Hz wav failed: {p.stderr.decode()[:300]}")
    return Path(dst_path)


def _read_mono(wav_path: Path, sr: int) -> np.ndarray:
    """Read a wav as mono float32, resampling defensively if needed."""
    import librosa
    audio, native_sr = soundfile.read(str(wav_path), dtype="float32")
    if getattr(audio, "ndim", 1) > 1:
        audio = audio.mean(axis=1)
    if native_sr != sr:
        audio = librosa.resample(audio, orig_sr=native_sr, target_sr=sr)
    return np.asarray(audio, dtype=np.float32)


def _chunk_wavs(wav_path: Path, sr: int, chunk_secs: float = 10.0) -> list[Path]:
    """Split a wav into <=chunk_secs pieces for embedding averaging."""
    audio = _read_mono(wav_path, sr)
    n = int(chunk_secs * sr)
    chunks = [audio[i:i + n] for i in range(0, len(audio), n)]
    # drop a tiny trailing tail (<1s) — too little signal for an embedding
    if len(chunks) > 1 and len(chunks[-1]) < sr:
        chunks = chunks[:-1]
    paths = []
    for i, ch in enumerate(chunks):
        cp = wav_path.with_name(f"{wav_path.stem}_c{i}.wav")
        soundfile.write(str(cp), ch, sr)
        paths.append(cp)
    return paths


def _extract_embedding(wav_path: Path):
    """Mean speaker embedding over chunks of a mono wav at converter rate."""
    conv = _get_converter()
    model, hps = conv["model"], conv["hps"]
    sr = int(hps.data.sampling_rate)
    gs = []
    for cp in _chunk_wavs(wav_path, sr):
        try:
            audio_ref = _read_mono(cp, sr)
            y = torch.FloatTensor(audio_ref).unsqueeze(0)
            y = spectrogram_torch(y, hps.data.filter_length, sr,
                                  hps.data.hop_length, hps.data.win_length,
                                  center=False)
            with torch.no_grad():
                g = model.ref_enc(y.transpose(1, 2)).unsqueeze(-1)
                gs.append(g.detach())
        finally:
            cp.unlink(missing_ok=True)
    if not gs:
        raise RuntimeError("no usable audio chunks for speaker embedding")
    return torch.stack(gs).mean(0)  # [1, 256, 1]


# --------------------------------------------------------------------------
# Cloned-voice store
# --------------------------------------------------------------------------
def _load_meta() -> dict:
    if META_PATH.exists():
        try:
            return json.loads(META_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_meta(meta: dict) -> None:
    CLONES_DIR.mkdir(parents=True, exist_ok=True)
    META_PATH.write_text(json.dumps(meta, indent=2))


def _sanitize_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("Voice name is empty.")
    return name[:40]


def create_cloned_voice(sample_path: str | Path, name: str) -> dict:
    """Validate a 6-30s sample, extract its embedding, register a clone.

    Returns {"voice_id": ..., "name": ...}. Raises ValueError/RuntimeError
    with a user-facing message on any problem.
    """
    name = _sanitize_name(name)
    dur = probe_duration_secs(sample_path)
    if dur < MIN_SAMPLE_SECS or dur > MAX_SAMPLE_SECS:
        raise ValueError(
            f"Sample is {dur:.1f}s — please upload {int(MIN_SAMPLE_SECS)}-"
            f"{int(MAX_SAMPLE_SECS)} seconds of clear speech.")
    with tempfile.TemporaryDirectory() as td:
        wav = to_target_wav(sample_path, Path(td) / "sample.wav")
        se = _extract_embedding(wav)  # [1, 256, 1] on cpu
    voice_id = "clone_" + uuid.uuid4().hex[:8]
    # avoid the (absurdly unlikely) id collision
    meta = _load_meta()
    while voice_id in meta:
        voice_id = "clone_" + uuid.uuid4().hex[:8]
    CLONES_DIR.mkdir(parents=True, exist_ok=True)
    torch.save(se.cpu(), CLONES_DIR / f"{voice_id}.pt")
    meta[voice_id] = {"name": name, "created": time.time(),
                      "sample_secs": round(dur, 1)}
    _save_meta(meta)
    log.info("created cloned voice %s (%s, %.1fs)", voice_id, name, dur)
    return {"voice_id": voice_id, "name": name}


def get_clone(voice_id: str) -> dict | None:
    meta = _load_meta()
    info = meta.get(voice_id)
    if not info:
        return None
    return {"voice_id": voice_id, **info}


def list_cloned_voices() -> list[dict]:
    meta = _load_meta()
    out = [{"voice_id": vid, "name": v["name"],
            "created": v.get("created", 0)} for vid, v in meta.items()]
    out.sort(key=lambda x: x["created"], reverse=True)
    return out


def delete_cloned_voice(voice_id: str) -> bool:
    meta = _load_meta()
    if voice_id not in meta:
        return False
    meta.pop(voice_id)
    _save_meta(meta)
    (CLONES_DIR / f"{voice_id}.pt").unlink(missing_ok=True)
    log.info("deleted cloned voice %s", voice_id)
    return True


def _load_embedding(voice_id: str):
    p = CLONES_DIR / f"{voice_id}.pt"
    if not p.exists():
        raise RuntimeError(f"cloned voice data missing for {voice_id}")
    return torch.load(str(p), map_location="cpu", weights_only=True)


# --------------------------------------------------------------------------
# Conversion: base TTS audio -> cloned voice
# --------------------------------------------------------------------------
def convert_voice(input_wav: str | Path, voice_id: str) -> np.ndarray:
    """Tone-convert a mono wav (at the converter's rate) to the cloned voice.

    Returns float32 numpy audio at the converter's sample rate. Raises
    RuntimeError on failure — callers must surface this as a job error,
    never silently fall back to the base voice.
    """
    info = get_clone(voice_id)
    if not info:
        raise RuntimeError(f"unknown cloned voice: {voice_id}")
    conv = _get_converter()
    model, hps = conv["model"], conv["hps"]
    sr = int(hps.data.sampling_rate)
    tgt_se = _load_embedding(voice_id)
    src_se = _extract_embedding(Path(input_wav))
    audio = _read_mono(Path(input_wav), sr)
    y = torch.FloatTensor(audio).unsqueeze(0)
    spec = spectrogram_torch(y, hps.data.filter_length, sr,
                             hps.data.hop_length, hps.data.win_length,
                             center=False)
    spec_lengths = torch.LongTensor([spec.size(-1)])
    with torch.no_grad():
        out = model.voice_conversion(spec, spec_lengths,
                                     sid_src=src_se, sid_tgt=tgt_se,
                                     tau=0.3)[0][0, 0].data.cpu().float().numpy()
    return out


def convert_mp3_voice(mp3_bytes: bytes, voice_id: str) -> bytes:
    """End-to-end: mp3 (any rate) -> cloned-voice mp3. Used by the job worker."""
    if not mp3_bytes:
        raise RuntimeError("empty audio for voice conversion")
    sr = _target_sr()
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "base.mp3"
        src.write_bytes(mp3_bytes)
        wav = to_target_wav(src, td / "base.wav")
        out = convert_voice(wav, voice_id)
        out_wav = td / "out.wav"
        soundfile.write(str(out_wav), out, sr)
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(out_wav), "-c:a", "libmp3lame", "-b:a", "48k",
             "-id3v2_version", "0", "-f", "mp3", str(td / "out.mp3")],
            capture_output=True, timeout=300)
        mp3 = (td / "out.mp3").read_bytes() if (td / "out.mp3").exists() else b""
        if p.returncode != 0 or len(mp3) < 1000:
            raise RuntimeError("could not encode converted audio to mp3")
        return mp3
