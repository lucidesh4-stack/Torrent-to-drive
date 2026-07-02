FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=production \
    PYTHONPATH=/app

WORKDIR /app

# System deps (curl for healthcheck, build-essential only if a wheel is missing)
RUN apt-get update && apt-get install -y --no-install-recommends curl gcc python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Create a non-privileged user and group for Hugging Face compatibility
RUN useradd -m -u 1000 user
WORKDIR /app

# Install Python deps first (done as root)
COPY streamly_optimized/requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy package and set ownership to user 1000
COPY --chown=user:user streamly_optimized /app/streamly_optimized
COPY --chown=user:user run.py /app/run.py

# Switch to the non-root user
USER user

# Hugging Face default port is 7860 (PORT env var is respected if set)
ENV PORT=7860
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

# Runs FastAPI app under Uvicorn
CMD ["sh", "-c", "uvicorn streamly_optimized.app:create_app --host 0.0.0.0 --port 7860"]
