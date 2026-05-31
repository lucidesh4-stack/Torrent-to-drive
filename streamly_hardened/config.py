from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class AppConfig:
    """Application configuration.

    Production deployments must inject this via environment/secret manager.
    Never commit real secrets. SECRET_KEY is intentionally mandatory outside tests.
    """

    secret_key: str
    environment: str = "production"
    request_timeout_seconds: float = 6.0
    archive_timeout_seconds: float = 10.0
    max_query_length: int = 128
    max_magnet_length: int = 8192
    max_folder_id: int = 9_007_199_254_740_991
    max_file_id: int = 9_007_199_254_740_991
    max_json_bytes: int = 16 * 1024
    max_bulk_items: int = 100  # Used by delete_bulk and zip_bulk
    session_ttl_seconds: int = 60 * 60 * 12
    client_store_max_entries: int = 100_000
    rate_limit_capacity: int = 60
    rate_limit_refill_per_second: float = 1.0
    allowed_categories: frozenset[str] = frozenset({"", "2", "3", "4", "5", "6", "7", "8", "9", "10"})
    allowed_sorts: frozenset[str] = frozenset({"relevance", "seeders", "leechers", "date", "size"})
    allowed_orders: frozenset[str] = frozenset({"asc", "desc"})
    bitsearch_url: str = "https://bitsearch.eu/api/v1/search"
    bitsearch_api_key: str = ""
    bitsearch_daily_limit: int = 200
    imdb_suggest_template: str = "https://v3.sg.media-imdb.com/suggestion/h/{query}.json"
    seedr_archive_url: str = "https://www.seedr.cc/api/v2/download/archive"
    upstash_redis_url: str = ""
    upstash_redis_token: str = ""
    seedr_email: str = ""
    seedr_password: str = ""

    @staticmethod
    def from_env() -> "AppConfig":
        env = os.getenv("APP_ENV", "production")
        secret = os.getenv("SECRET_KEY")
        upstash_url = os.getenv("UPSTASH_REDIS_REST_URL", "")
        upstash_token = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
        if not secret and env != "test":
            # In hosted dev/prod without explicit SECRET_KEY: derive from Upstash if available,
            # else generate ephemeral (logs out users on restart — acceptable for solo/free tier).
            if upstash_url and upstash_token:
                from .redis_store import RedisStore
                rs = RedisStore(upstash_url, upstash_token)
                secret = rs.get_or_create_secret()
            else:
                import secrets as _secrets
                secret = _secrets.token_hex(32)
        return AppConfig(
            secret_key=secret or "test-only-not-for-production",
            environment=env,
            request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "6")),
            archive_timeout_seconds=float(os.getenv("ARCHIVE_TIMEOUT_SECONDS", "10")),
            session_ttl_seconds=int(os.getenv("SESSION_TTL_SECONDS", str(60 * 60 * 12))),
            upstash_redis_url=upstash_url,
            upstash_redis_token=upstash_token,
            seedr_email=os.getenv("SEEDR_EMAIL", ""),
            seedr_password=os.getenv("SEEDR_PASSWORD", ""),
            bitsearch_url=os.getenv("BITSEARCH_URL", "https://bitsearch.eu/api/v1/search"),
            bitsearch_api_key=os.getenv("BITSEARCH_API_KEY", ""),
            bitsearch_daily_limit=int(os.getenv("BITSEARCH_DAILY_LIMIT", "200")),
            max_bulk_items=int(os.getenv("MAX_BULK_ITEMS", "100")),
        )
