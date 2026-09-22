"""
Raw-socket websocket TTS client speaking the edge-tts wire protocol.

Why this exists: the server's egress proxy rewrites the websocket handshake
response header "Connection: Upgrade" -> "Connection: close". aiohttp
strictly requires "Upgrade" and then drops the (actually still-alive)
connection, so edge_tts.Communicate can never stream here. This module
opens the proxy CONNECT tunnel and TLS session manually and speaks the
websocket framing + edge-tts message protocol directly over a blocking
socket. Run it in a thread (asyncio.to_thread) from async code.

No third-party deps beyond edge_tts (used only for DRM token helpers and
SSML formatting constants — no network through edge_tts itself).
"""

import base64
import hashlib
import os
import socket
import ssl
import struct
import time
from urllib.parse import urlparse
from xml.sax.saxutils import escape as _xml_escape

import certifi

HOST = "speech.platform.bing.com"
WSS_PATH = "/consumer/speech/synthesize/readaloud/edge/v1"
TRUSTED_CLIENT_TOKEN = "6A5AA1D4EAFF4E9FB37E23D68491D6F4"
CONNECT_TIMEOUT = 20
SOCKET_TIMEOUT = 60


def _tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certifi.where())
    ca = os.environ.get("SSL_CERT_FILE")
    if ca and os.path.exists(ca):
        ctx.load_verify_locations(cafile=ca)
    return ctx


def _proxy() -> tuple:
    """Return (host, port, auth_header_value_or_None) for the egress proxy."""
    raw = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if not raw:
        return None, None, None
    pu = urlparse(raw)
    auth = None
    if pu.username:
        token = base64.b64encode(
            f"{pu.username}:{pu.password or ''}".encode()
        ).decode()
        auth = f"Basic {token}"
    return pu.hostname, pu.port, auth


def _recvall(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed while reading")
        buf += chunk
    return buf


def _read_http_response(sock: socket.socket) -> tuple:
    """Read until end of HTTP headers. Returns (status_line, headers_dict)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    head = buf.split(b"\r\n\r\n", 1)[0].decode("latin1")
    lines = head.split("\r\n")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return lines[0], headers


class _RawWebSocket:
    """Minimal blocking websocket client (text/binary/ping/pong/close)."""

    def __init__(self, sock: socket.socket):
        self.sock = sock

    def send_text(self, text: str) -> None:
        self._send(0x1, text.encode("utf-8"))

    def _send(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        n = len(payload)
        header = bytes([0x80 | opcode])
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def _send_pong(self, payload: bytes) -> None:
        self._send(0xA, payload)

    def recv_message(self) -> tuple:
        """Returns (opcode, payload_bytes). Raises on close/error."""
        chunks = []
        opcode = None
        while True:
            b1, b2 = _recvall(self.sock, 2)
            fin = b1 & 0x80
            op = b1 & 0x0F
            masked = b2 & 0x80
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", _recvall(self.sock, 2))[0]
            elif length == 127:
                length = struct.unpack(">Q", _recvall(self.sock, 8))[0]
            if masked:
                mask = _recvall(self.sock, 4)
            payload = _recvall(self.sock, length) if length else b""
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if op == 0x8:
                raise ConnectionError("server closed websocket")
            if op == 0x9:
                self._send_pong(payload)
                continue
            if op == 0xA:
                continue
            if op in (0x1, 0x2):
                if opcode is None:
                    opcode = op
                chunks.append(payload)
                if fin:
                    return opcode, b"".join(chunks)
            # continuation frames (0x0) fall through and append

    def close(self) -> None:
        try:
            self._send(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _ws_handshake(sock: socket.socket) -> None:
    from edge_tts.communicate import WSS_HEADERS, connect_id
    from edge_tts.drm import DRM

    key = base64.b64encode(os.urandom(16)).decode()
    path = (
        f"{WSS_PATH}?TrustedClientToken={TRUSTED_CLIENT_TOKEN}"
        f"&ConnectionId={connect_id()}"
        f"&Sec-MS-GEC={DRM.generate_sec_ms_gec()}"
        f"&Sec-MS-GEC-Version=1-143.0.3650.75"
    )
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {HOST}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"User-Agent: {WSS_HEADERS['User-Agent']}\r\n"
        "\r\n"
    )
    sock.sendall(req.encode())
    status, headers = _read_http_response(sock)
    if "101" not in status:
        raise ConnectionError(f"websocket handshake failed: {status}")
    accept = headers.get("sec-websocket-accept", "")
    expected = base64.b64encode(
        hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
        ).digest()
    ).decode()
    if accept != expected:
        raise ConnectionError("websocket accept key mismatch")


def _date_string() -> str:
    return time.strftime(
        "%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()
    )


def _remove_incompatible(text: str) -> str:
    # same set edge_tts strips
    return "".join(
        c for c in text if not (0xE000 <= ord(c) <= 0xF8FF)
    )


def synthesize_sync(text: str, voice: str, rate: str = "+0%", pitch: str = "+0Hz",
                    volume: str = "+0%") -> bytes:
    """Generate mp3 audio bytes for text. Blocking — call via asyncio.to_thread.

    NOTE: `text` must be plain text. The endpoint rejects any SSML element
    inside <prosody> (break/express-as/nested prosody all drop the
    connection), so emotion features are implemented segment-wise by the
    caller (see parse_emotion_script) — never by embedding tags here.
    """
    proxy_host, proxy_port, proxy_auth = _proxy()
    if proxy_host:
        sock = socket.create_connection((proxy_host, proxy_port), timeout=CONNECT_TIMEOUT)
        connect_req = (
            f"CONNECT {HOST}:443 HTTP/1.1\r\n"
            f"Host: {HOST}:443\r\n"
            + (f"Proxy-Authorization: {proxy_auth}\r\n" if proxy_auth else "")
            + "\r\n"
        )
        sock.sendall(connect_req.encode())
        status, _ = _read_http_response(sock)
        if "200" not in status:
            sock.close()
            raise ConnectionError(f"proxy CONNECT failed: {status}")
    else:
        sock = socket.create_connection((HOST, 443), timeout=CONNECT_TIMEOUT)

    sock.settimeout(SOCKET_TIMEOUT)
    try:
        tls = _tls_context().wrap_socket(sock, server_hostname=HOST)
    except Exception:
        sock.close()
        raise

    try:
        _ws_handshake(tls)
        ws = _RawWebSocket(tls)

        inner = _xml_escape(_remove_incompatible(text))
        ssml = (
            "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='en-US'>"
            f"<voice name='{voice}'>"
            f"<prosody pitch='{pitch}' rate='{rate}' volume='{volume}'>"
            f"{inner}"
            "</prosody></voice></speak>"
        )
        request_id = "".join(f"{b:02x}" for b in os.urandom(16))

        ws.send_text(
            f"X-Timestamp:{_date_string()}\r\n"
            "Content-Type:application/json; charset=utf-8\r\n"
            "Path:speech.config\r\n\r\n"
            '{"context":{"synthesis":{"audio":{"metadataoptions":{'
            '"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"false"},'
            '"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n'
        )
        ws.send_text(
            f"X-RequestId:{request_id}\r\n"
            "Content-Type:application/ssml+xml\r\n"
            f"X-Timestamp:{_date_string()}Z\r\n"
            "Path:ssml\r\n\r\n"
            f"{ssml}"
        )

        audio = bytearray()
        while True:
            opcode, payload = ws.recv_message()
            if opcode == 0x1:  # text
                if b"Path:turn.end" in payload:
                    break
            elif opcode == 0x2:  # binary: 2-byte header len + header + audio
                if len(payload) > 2:
                    header_len = int.from_bytes(payload[:2], "big")
                    audio += payload[header_len + 2:]
        ws.close()
    finally:
        try:
            tls.close()
        except OSError:
            pass

    if not audio:
        raise RuntimeError("no audio received from voice service")
    return bytes(audio)




# ---------------------------------------------------------------------------
# Emotion / expression controls (segment-based)
#
# NOTE (2026-09-21): the readaloud endpoint rejects ANY SSML element inside
# <prosody> — <break/>, nested <prosody>, <mstts:express-as> all make the
# server close the websocket immediately (verified with 5 variants). So
# "SSML passthrough" is impossible here; emotions are implemented as
# functional equivalents instead:
#
#   [sans]/[breathe] -> 600ms silence gap   (== <break time="600ms"/>)
#   [ruko]/[pause]   -> 1200ms silence gap  (== <break time="1200ms"/>)
#   [ahista]/[slow]  -> segment rendered 35% slower (== prosody rate)
#   [tez]/[fast]     -> segment rendered 35% faster
#   [hansna]/[laugh] -> segment brighter: slightly faster + higher pitch
#                        (true "cheerful" style not available; approximation)
#   [rona]/[cry]     -> segment somber: slightly slower + lower pitch
#                        (true "sad" style not available; approximation)
#
# An emotion marker applies to the text following it, up to the next marker.
# "Auto emotions" inserts a 600ms silence gap at paragraph breaks; commas and
# semicolons already produce natural pauses in the engine, so they are left
# alone (extra gaps there would sound robotic).
# ---------------------------------------------------------------------------
import re as _re

_MARKER_RE = _re.compile(
    r"\[(sans|breathe|ruko|pause|hansna|laugh|rona|cry|ahista|slow|tez|fast)\]",
    _re.IGNORECASE,
)

_PAUSE_MS = {"sans": 600, "breathe": 600, "ruko": 1200, "pause": 1200}

_EMOTION_KIND = {
    "hansna": "laugh", "laugh": "laugh",
    "rona": "cry", "cry": "cry",
    "ahista": "slow", "slow": "slow",
    "tez": "fast", "fast": "fast",
}

# per-segment prosody adjustments (deltas combined with the user's rate/pitch)
EMOTION_ADJUST = {
    "laugh": {"rate_delta": +10, "pitch_delta": +12},
    "cry":   {"rate_delta": -15, "pitch_delta": -12},
    "slow":  {"rate_delta": -35, "pitch_delta": 0},
    "fast":  {"rate_delta": +35, "pitch_delta": 0},
}

_AUTO_PARA = _re.compile(r"\n\s*\n|\n")


def parse_emotion_script(text: str, auto_emotions: bool = True) -> list:
    """Parse user text into a list of ops.

    Each op is ("say", kind|None, text) or ("pause", milliseconds).
    kind is one of laugh/cry/slow/fast. Markers are consumed.
    """
    ops: list = []
    parts = _MARKER_RE.split(text)
    open_kind = None
    buf: list = []

    def flush():
        t = "".join(buf).strip()
        buf.clear()
        if t:
            ops.append(("say", open_kind, t))

    for i, part in enumerate(parts):
        if i % 2 == 1:  # a marker token
            key = part.lower()
            if key in _PAUSE_MS:
                flush()
                ops.append(("pause", _PAUSE_MS[key]))
            else:  # emotion wrapper applies to following text
                flush()
                open_kind = _EMOTION_KIND[key]
            continue
        seg = _remove_incompatible(part)
        if seg:
            buf.append(seg)
    flush()

    if auto_emotions:
        out: list = []
        for op in ops:
            if op[0] == "say":
                paras = [p.strip() for p in _AUTO_PARA.split(op[2]) if p.strip()]
                for j, p in enumerate(paras):
                    if j:
                        out.append(("pause", 600))
                    out.append(("say", op[1], p))
            else:
                out.append(op)
        ops = out
    return [op for op in ops if not (op[0] == "say" and not op[2].strip())]


# ---------------------------------------------------------------------------
# Word-boundary capture (added 2026-09-21 for karaoke captions)
#
# synthesize_with_words() mirrors synthesize_sync() but requests
# wordBoundaryEnabled metadata and returns per-word timings alongside the
# audio. Offsets/durations arrive in 100ns ticks relative to the start of
# THIS segment's audio. Raises the same errors as synthesize_sync.
# ---------------------------------------------------------------------------
import json as _json


def synthesize_with_words(text: str, voice: str, rate: str = "+0%",
                          pitch: str = "+0Hz", volume: str = "+0%"):
    """Returns (mp3_bytes, words) where words = [(word, start_s, end_s)]."""
    proxy_host, proxy_port, proxy_auth = _proxy()
    if proxy_host:
        sock = socket.create_connection((proxy_host, proxy_port),
                                        timeout=CONNECT_TIMEOUT)
        connect_req = (
            f"CONNECT {HOST}:443 HTTP/1.1\r\n"
            f"Host: {HOST}:443\r\n"
            + (f"Proxy-Authorization: {proxy_auth}\r\n" if proxy_auth else "")
            + "\r\n"
        )
        sock.sendall(connect_req.encode())
        status, _ = _read_http_response(sock)
        if "200" not in status:
            sock.close()
            raise ConnectionError(f"proxy CONNECT failed: {status}")
    else:
        sock = socket.create_connection((HOST, 443), timeout=CONNECT_TIMEOUT)

    sock.settimeout(SOCKET_TIMEOUT)
    try:
        tls = _tls_context().wrap_socket(sock, server_hostname=HOST)
    except Exception:
        sock.close()
        raise

    words = []
    try:
        _ws_handshake(tls)
        ws = _RawWebSocket(tls)

        inner = _xml_escape(_remove_incompatible(text))
        ssml = (
            "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='en-US'>"
            f"<voice name='{voice}'>"
            f"<prosody pitch='{pitch}' rate='{rate}' volume='{volume}'>"
            f"{inner}"
            "</prosody></voice></speak>"
        )
        request_id = "".join(f"{b:02x}" for b in os.urandom(16))

        ws.send_text(
            f"X-Timestamp:{_date_string()}\r\n"
            "Content-Type:application/json; charset=utf-8\r\n"
            "Path:speech.config\r\n\r\n"
            '{"context":{"synthesis":{"audio":{"metadataoptions":{'
            '"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"true"},'
            '"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n'
        )
        ws.send_text(
            f"X-RequestId:{request_id}\r\n"
            "Content-Type:application/ssml+xml\r\n"
            f"X-Timestamp:{_date_string()}Z\r\n"
            "Path:ssml\r\n\r\n"
            f"{ssml}"
        )

        audio = bytearray()
        while True:
            opcode, payload = ws.recv_message()
            if opcode == 0x1:  # text
                if b"Path:turn.end" in payload:
                    break
                if b"Path:audio.metadata" in payload:
                    try:
                        body = payload.split(b"\r\n\r\n", 1)[1]
                        meta = _json.loads(body.decode("utf-8"))
                        for m in meta.get("Metadata", []):
                            if m.get("Type") != "WordBoundary":
                                continue
                            d = m.get("Data", {})
                            w = (d.get("text") or {}).get("Text", "")
                            if not w:
                                continue
                            start = d.get("Offset", 0) / 1e7
                            dur = d.get("Duration", 0) / 1e7
                            words.append((w, start, start + dur))
                    except Exception:
                        pass
            elif opcode == 0x2:  # binary audio
                if len(payload) > 2:
                    header_len = int.from_bytes(payload[:2], "big")
                    audio += payload[header_len + 2:]
        ws.close()
    finally:
        try:
            tls.close()
        except OSError:
            pass

    if not audio:
        raise RuntimeError("no audio received from voice service")
    return bytes(audio), words
