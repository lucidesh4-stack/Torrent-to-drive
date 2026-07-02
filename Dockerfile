FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=production

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl gcc python3-dev \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user
WORKDIR /app

# USE THE OPTIMIZED REQUIREMENTS
COPY streamly/requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt

# USE THE OPTIMIZED PACKAGE
COPY --chown=user:user streamly /app/streamly

USER user

ENV PORT=7860
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

# RUN THE OPTIMIZED FASTAPI APP
CMD ["sh", "-c", "uvicorn streamly.app:create_app --host 0.0.0.0 --port ${PORT} --workers 1"]
