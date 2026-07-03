# ---- Stage 1: builder ---------------------------------------------------
# Compiles/installs Python dependencies (some, like cryptg, may need gcc/headers
# if no matching prebuilt wheel exists for the target platform). Nothing from
# this stage ships in the final image except the installed site-packages.
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY streamly/requirements.txt /app/requirements.txt
RUN pip install --upgrade pip \
    && pip install --prefix=/install -r requirements.txt

# ---- Stage 2: final runtime image ----------------------------------------
# No compilers/headers here -- only curl (for the HEALTHCHECK) and the
# already-built Python packages copied in from the builder stage.
FROM python:3.11-slim AS final

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=production

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user

# Bring in the packages built in the builder stage (no gcc/python3-dev needed here)
COPY --from=builder /install /usr/local

# Copy the application code
COPY --chown=user:user streamly /app/streamly

USER user

ENV PORT=7860
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

CMD ["sh", "-c", "uvicorn streamly.app:create_app --factory --host 0.0.0.0 --port ${PORT} --workers 1"]
