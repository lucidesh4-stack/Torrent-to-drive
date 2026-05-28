FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=production

WORKDIR /app

# System deps (curl for healthcheck, build-essential only if a wheel is missing)
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (better layer caching)
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy package (build context = streamly_hardened/, mounted at /app/streamly_hardened)
COPY . /app/streamly_hardened

# Render injects PORT; default to 10000 for local docker runs
ENV PORT=10000
EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

# 2 workers x 4 threads fits Render's 512MB free tier comfortably
CMD ["sh", "-c", "gunicorn --workers 2 --threads 4 --timeout 60 --bind 0.0.0.0:${PORT} 'streamly_hardened.app:create_app()'"]
