# Goonj API — Dockerfile (Railway)
# Build context: repository root of the `goonj` GitHub repo, which contains:
#   index.html, app.js, voices.js, style.css   (GitHub Pages static site)
#   backend/                                    (this FastAPI API)
#   requirements.txt
#   Dockerfile                                  (this file)
FROM python:3.11-slim

# ffmpeg renders silence gaps, probes audio and powers voice-clone conversion.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU-only torch FIRST via the PyTorch CPU index: the default PyPI index
# would pull multi-GB CUDA wheels that don't fit / can't run here.
RUN pip install --no-cache-dir torch torchaudio \
    --index-url https://download.pytorch.org/whl/cpu

# Install Python deps (cached layer) ...
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ... then the API itself. (The static frontend lives at the repo root for
# GitHub Pages; only the API is deployed here.)
COPY backend/ ./backend/

# Pre-download the OpenVoice v2 converter checkpoint (~125 MB) at BUILD time
# so the first clone request never waits on a download. A download failure
# fails the build LOUDLY (non-zero exit) — better than a silently broken
# runtime that only errors when a user tries cloning.
RUN python -c "from backend.voice_clone import ensure_models; ensure_models()"

ENV PORT=8765

EXPOSE 7860

# Shell form so $PORT expands at container start (Railway injects $PORT).
CMD uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8765}
