# Streamly Hardened Reference

This is a security-focused reference rewrite of the uploaded Flask app.

## Run locally

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r streamly_hardened/requirements.txt
export APP_ENV=development
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m streamly_hardened.app
```

## Test

```bash
pip install -r streamly_hardened/requirements.txt
pytest streamly_hardened/tests
```

## Production notes

- Replace `TTLStore` with Redis/Dragonfly; do not rely on process-local sessions across workers/regions.
- Store Seedr tokens with envelope encryption and a KMS, not raw credentials.
- Put the app behind TLS, WAF, API gateway rate limiting, centralized logs, metrics, and tracing.
- Run with Gunicorn/Uvicorn workers; never `app.run(debug=True)` in production.
