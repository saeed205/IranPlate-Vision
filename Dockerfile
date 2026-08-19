FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLATE_DB=/data/traffic.db \
    HF_HOME=/cache/huggingface

WORKDIR /app

# ffmpeg is needed for RTSP; the lib* packages are OpenCV's runtime deps.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# Run unprivileged, and give the app writable homes for the DB and model cache.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data /cache \
    && chown -R appuser:appuser /data /cache /app
USER appuser

EXPOSE 5000

# waitress, not app.py: Flask's built-in server is explicitly not for production.
# Threads are modest on purpose — inference is serialised by a lock anyway.
CMD ["waitress-serve", "--host=0.0.0.0", "--port=5000", "--threads=8", "app:app"]
