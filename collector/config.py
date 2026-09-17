"""Environment configuration & salt variables.

All settings are overridable via environment variables:

    DATABASE_URL   Async SQLAlchemy URL. Defaults to SQLite in ./data/.
                   SQLite:      sqlite+aiosqlite:///./data/statless.db
                   PostgreSQL:  postgresql+asyncpg://user:pass@db:5432/statless
    GEOIP_DB_PATH  Path to MaxMind GeoLite2-Country.mmdb (default: data/GeoLite2-Country.mmdb)
    SALT_ROTATE_HOURS  Hours between daily salt rotations (default: 24, must be > 0)
    BASE_URL       Public base URL used to render embed/pixel snippets (default: http://localhost:8000)
    TRUST_PROXY    Trust X-Forwarded-For / X-Real-IP headers. Enable ONLY behind a reverse
                   proxy, otherwise clients can spoof IPs via headers (default: false)
    RATE_LIMIT     Max tracked events per minute per client (0 disables, default: 120)
    STATLESS_HOST / STATLESS_PORT  Bind address for the `statless` entrypoint
                   (namespaced to avoid colliding with shell/CI HOST & PORT; defaults 0.0.0.0 / 8000)
    STATS_TOKEN    When set (non-empty), GET /stats requires ?token=<value>.
                   Empty (default) keeps stats public — the embed badge links to them.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    database_url: str = f"sqlite+aiosqlite:///{ROOT / 'data' / 'statless.db'}"
    geoip_db_path: str = str(ROOT / "data" / "GeoLite2-Country.mmdb")
    salt_rotate_hours: float = Field(default=24.0, gt=0)
    base_url: str = "http://localhost:8000"
    trust_proxy: bool = False
    rate_limit: int = Field(default=120, ge=0)
    stats_token: str = ""
    host: str = Field(default="0.0.0.0", validation_alias="STATLESS_HOST")
    port: int = Field(default=8000, ge=1, le=65535, validation_alias="STATLESS_PORT")


@lru_cache
def get_settings() -> Settings:
    return Settings()
