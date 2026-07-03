from __future__ import annotations

import os
import secrets as _secrets
from typing import Tuple, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    """Application configuration.

    Production deployments must inject this via environment/secret manager.
    Never commit real secrets. SECRET_KEY is intentionally mandatory outside tests.
    """

    secret_key: str = "test-only-not-for-production"
    environment: str = "production"
    request_timeout_seconds: float = 6.0
    archive_timeout_seconds: float = 10.0
    max_query_length: int = 128
    max_magnet_length: int = 8192
    max_folder_id: int = 9_007_199_254_740_991
    max_file_id: int = 9_007_199_254_740_991
    max_json_bytes: int = 16 * 1024
    session_ttl_seconds: int = 60 * 60 * 12
    client_store_max_entries: int = 100_000
    rate_limit_capacity: int = 60
    rate_limit_refill_per_second: float = 1.0
    bitsearch_url: str = "https://bitsearch.eu/api/v1/search"
    # Torrent search providers tried in PRIORITY ORDER (failover, not merge):
    # multi_search uses the FIRST provider that returns results, so normal
    # operation draws from a single source (no cross-source duplicates).
    # bitsearch first, then apibay, then torrents-csv. Configurable via
    # SEARCH_PROVIDERS env (comma-separated, in priority order).
    search_providers: Tuple[str, ...] = ("bitsearch", "apibay", "torrents-csv")
    imdb_suggest_template: str = "https://v3.sg.media-imdb.com/suggestion/h/{query}.json"
    upstash_redis_url: str = ""
    upstash_redis_token: str = ""
    seedr_email: str = ""
    seedr_password: str = ""
    telegram_api_id: Optional[int] = None
    telegram_api_hash: str = ""
    telegram_phone: str = ""
    telegram_chat_id: str = "-1004247146382"
    cloudflare_worker_proxy: str = "https://streamly-proxy.lucidesh.workers.dev"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    def from_env(cls) -> AppConfig:
        env = os.getenv("APP_ENV", "production")
        secret = os.getenv("SECRET_KEY")
        upstash_url = os.getenv("UPSTASH_REDIS_REST_URL", "")
        upstash_token = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
        if not secret and env == "test":
            secret = _secrets.token_hex(32)
        if not secret and env != "test":
            # SECRET_KEY MUST be set explicitly outside test mode. Previously this
            # silently derived a key from Upstash via a synchronous, blocking
            # `requests.post()` call (up to ~6s worst case across two round-trips)
            # made at process startup, before uvicorn's event loop even exists --
            # or, if Upstash wasn't configured, silently generated an ephemeral key
            # every restart (logging out every session with no warning). Failing
            # fast here instead surfaces a clear, actionable error immediately
            # rather than a confusing runtime symptom (e.g. "why did everyone get
            # logged out?") days or weeks later.
            raise RuntimeError(
                "SECRET_KEY environment variable is required outside test mode "
                "(APP_ENV=test). Set it explicitly as a secret in your deployment "
                "environment (e.g. Hugging Face Space secrets) -- for example: "
                f"SECRET_KEY={_secrets.token_hex(32)!r} (generate your own; do not "
                "reuse this example value). Auto-derivation via Upstash has been "
                "removed: it made a blocking network call at startup and provided "
                "no real security benefit over just setting the value directly."
            )
        
        tg_id_raw = os.getenv("TELEGRAM_API_ID", "")
        tg_id = int(tg_id_raw) if tg_id_raw.isdigit() else None
        
        providers_raw = os.getenv("SEARCH_PROVIDERS", "bitsearch,apibay,torrents-csv")
        providers = tuple(
            p.strip() for p in providers_raw.split(",") if p.strip()
        ) or ("bitsearch", "apibay", "torrents-csv")
        
        return cls(
            secret_key=secret or "test-only-not-for-production",
            environment=env,
            request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "6.0")),
            archive_timeout_seconds=float(os.getenv("ARCHIVE_TIMEOUT_SECONDS", "10.0")),
            session_ttl_seconds=int(os.getenv("SESSION_TTL_SECONDS", str(60 * 60 * 12))),
            upstash_redis_url=upstash_url,
            upstash_redis_token=upstash_token,
            seedr_email=os.getenv("SEEDR_EMAIL", ""),
            seedr_password=os.getenv("SEEDR_PASSWORD", ""),
            telegram_api_id=tg_id,
            telegram_api_hash=os.getenv("TELEGRAM_API_HASH") or os.getenv("TELEGRAM_api_hash") or "",
            telegram_phone=os.getenv("TELEGRAM_PHONE", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "-1004247146382"),
            cloudflare_worker_proxy=os.getenv("CLOUDFLARE_WORKER_PROXY", "").strip() or "https://streamly-proxy.lucidesh.workers.dev",
            bitsearch_url=os.getenv("BITSEARCH_URL", "https://bitsearch.eu/api/v1/search"),
            search_providers=providers,
        )
