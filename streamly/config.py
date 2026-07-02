from __future__ import annotations
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Tuple

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    secret_key: str = Field(default="test-only-not-for-production", validation_alias="SECRET_KEY")
    app_env: str = Field(default="production", validation_alias="APP_ENV")
    request_timeout_seconds: float = Field(default=6.0, validation_alias="REQUEST_TIMEOUT_SECONDS")
    archive_timeout_seconds: float = Field(default=10.0, validation_alias="ARCHIVE_TIMEOUT_SECONDS")
    max_query_length: int = 128
    max_magnet_length: int = 8192
    max_folder_id: int = 9_007_199_254_740_991
    max_file_id: int = 9_007_199_254_740_991
    max_json_bytes: int = 16 * 1024
    session_ttl_seconds: int = 60 * 60 * 12
    client_store_max_entries: int = 100_000
    rate_limit_capacity: int = 60
    rate_limit_refill_per_second: float = 1.0
    bitsearch_url: str = Field(default="https://bitsearch.eu/api/v1/search", validation_alias="BITSEARCH_URL")
    search_providers: Tuple[str, ...] = ("bitsearch", "apibay", "torrents-csv")
    imdb_suggest_template: str = "https://v3.sg.media-imdb.com/suggestion/h/{query}.json"
    upstash_redis_url: str = Field(default="", validation_alias="UPSTASH_REDIS_REST_URL")
    upstash_redis_token: str = Field(default="", validation_alias="UPSTASH_REDIS_REST_TOKEN")
    seedr_email: str = Field(default="", validation_alias="SEEDR_EMAIL")
    seedr_password: str = Field(default="", validation_alias="SEEDR_PASSWORD")
    telegram_api_id: int | None = Field(default=None, validation_alias="TELEGRAM_API_ID")
    telegram_api_hash: str = Field(default="", validation_alias="TELEGRAM_API_HASH")
    telegram_phone: str = Field(default="", validation_alias="TELEGRAM_PHONE")
    telegram_chat_id: str = Field(default="-1004247146382", validation_alias="TELEGRAM_CHAT_ID")
    cloudflare_worker_proxy: str = Field(default="https://streamly-proxy.lucidesh.workers.dev", validation_alias="CLOUDFLARE_WORKER_PROXY")

    @classmethod
    def load(cls):
        # Handle the comma-separated search_providers env var if it exists
        import os
        sp_env = os.getenv("SEARCH_PROVIDERS")
        settings = cls()
        if sp_env:
            settings.search_providers = tuple(p.strip() for p in sp_env.split(",") if p.strip())
        return settings

settings = Settings.load()
