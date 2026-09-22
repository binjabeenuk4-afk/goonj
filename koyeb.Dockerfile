# Goonj API — Koyeb Dockerfile
# Build context: repository root of the `goonj` GitHub repo, which contains:
#   index.html, app.js, voices.js, style.css   (GitHub Pages static site)
#   backend/                                    (this FastAPI API)
#   requirements.txt
#   koyeb.Dockerfile                            (this file)
FROM python:3.11-slim

# ffmpeg renders the short silence gaps for [sans]/[ruko] and podcast pauses.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/

# Koyeb injects $PORT at runtime; uvicorn binds to it.
CMD uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8765}
