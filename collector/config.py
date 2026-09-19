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
    EMBED_ALLOWED_ORIGINS  Extra origins allowed to frame the embed widget, comma-separated
                   (default: Notion apex + wildcard domains). Wildcards never match the apex
                   domain - add both when you need both.
    STATLESS_HOST / STATLESS_PORT  Bind address for the `statless` entrypoint
                   (namespaced to avoid colliding with shell/CI HOST & PORT; defaults
                   0.0.0.0 / 8000)
    STATS_TOKEN    When set (non-empty), GET /stats, /overview, and /export require
                   ?token=<value> or the X-Stats-Token header (header preferred - query
                   strings end up in access logs). Also gates DELETE /docs/{doc_key}
                   (Art. 17). Empty (default) keeps stats public - the embed badge
                   links to them.
    SECURITY_CONTACT  Contact line for /.well-known/security.txt. Replace the mailto
                   placeholder before going public (default: mailto:security@YOUR-DOMAIN.example)
    SECURITY_POLICY  Optional Policy URL for /.well-known/security.txt (e.g. your VDP or ToS)
    SECURITY_EXPIRES Optional RFC 3339 timestamp for security.txt Expires field
                   (default: 1 year ahead)
    CONTROLLER_NAME  Optional controller name for the /privacy notice
    CONTROLLER_CONTACT Optional controller contact link or email for the /privacy notice
    RETENTION_DAYS Delete events older than this many days (default: 180).
                   0 disables automatic deletion (data is kept indefinitely -
                   you then own the GDPR storage-limitation duty yourself).
    VIEW_DEDUPE_MINUTES  Collapse repeated views of the same page by the same
                   IP-hash within the window (default: 0 = every fetch counts).
    FILTER_BOTS    Skip crawlers, link-preview/unfurl bots, headless browsers,
                   and preview/prefetch fetches so counts reflect humans
                   (default: true).
    SERVER_SECRET  Optional. When set, the IP-hash salt is derived deterministically
                   (HMAC of the UTC date), so uniques survive restarts and can be
                   reproduced across replicas sharing the secret.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, NonNegativeInt, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]

# Apex domains matter: *.notion.so never matches https://notion.so itself.
DEFAULT_EMBED_ORIGINS: tuple[str, ...] = (
    "https://notion.so",
    "https://*.notion.so",
    "https://notion.site",
    "https://*.notion.site",
)
# https origin, optional port; subdomain wildcard must be a full leading label.
# A leading '*.' wildcard never matches the apex domain - list both when you need both.
EMBED_ORIGIN_RE = re.compile(
    r"^https://(\*\.)?[A-Za-z0-9][A-Za-z0-9-]*(\.[A-Za-z0-9][A-Za-z0-9-]*)*(:\d{1,5})?$"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    database_url: str = f"sqlite+aiosqlite:///{ROOT / 'data' / 'statless.db'}"
    geoip_db_path: str = str(ROOT / "data" / "GeoLite2-Country.mmdb")
    salt_rotate_hours: float = Field(default=24.0, gt=0)
    base_url: str = "http://localhost:8000"
    security_contact: str = "mailto:security@YOUR-DOMAIN.example"
    security_policy: str = ""
    security_expires: str = ""
    controller_name: str = ""
    controller_contact: str = ""
    trust_proxy: bool = False
    rate_limit: int = Field(default=120, ge=0)
    stats_token: str = ""
    retention_days: NonNegativeInt = 180
    view_dedupe_minutes: NonNegativeInt = 0
    filter_bots: bool = True
    server_secret: str = ""
    embed_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_EMBED_ORIGINS)
    )
    host: str = Field(default="0.0.0.0", validation_alias="STATLESS_HOST")
    port: int = Field(default=8000, ge=1, le=65535, validation_alias="STATLESS_PORT")

    @field_validator("embed_allowed_origins", mode="before")
    @classmethod
    def _split_embed_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("embed_allowed_origins", mode="after")
    @classmethod
    def _validate_embed_origins(cls, value: list[str]) -> list[str]:
        deduped: list[str] = []
        for origin in value:
            if origin in deduped:
                continue
            if not EMBED_ORIGIN_RE.match(origin):
                raise ValueError(
                    f"invalid embed origin {origin!r}: use an https origin like "
                    "https://example.com; a leading '*.' wildcard never matches the apex"
                )
            deduped.append(origin)
        return deduped


@lru_cache
def get_settings() -> Settings:
    return Settings()
