# ---- Stage 1: builder ---------------------------------------------------
# Installs Python dependencies into a relocatable prefix. For the CURRENT pinned
# requirements on python:3.11-slim (x86_64), EVERY dependency — including the
# C-extension ones (cryptg, uvloop, httptools) — ships a prebuilt cp311
# manylinux wheel, and the only sdist-only dep (pyaes, pulled in by telethon) is
# pure-Python. So NO C compiler is needed. `gcc`/`python3-dev` were therefore
# dead build weight and have been removed (O-6).
#
# --only-binary=:all: with an explicit allow for the one known pure-Python sdist
# (pyaes) makes this guarantee enforceable: if a future dependency bump introduces
# a package that would need to COMPILE, the build FAILS LOUDLY here instead of
# silently regressing to needing a toolchain we no longer install.
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY streamly/requirements.txt /app/requirements.txt
RUN pip install --upgrade pip \
    && pip install --prefix=/install --prefer-binary \
       --only-binary=:all: --no-binary=pyaes \
       -r requirements.txt

# ---- Stage 2: final runtime image ----------------------------------------
# Only curl (for the HEALTHCHECK) plus the already-installed Python packages.
FROM python:3.11-slim AS final

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=production

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user

# Bring in the packages built in the builder stage
COPY --from=builder /install /usr/local

# Copy the application code
COPY --chown=user:user streamly /app/streamly

USER user

ENV PORT=7860
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

CMD ["sh", "-c", "uvicorn streamly.app:create_app --factory --host 0.0.0.0 --port ${PORT} --workers 1"]
