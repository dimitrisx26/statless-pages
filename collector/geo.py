"""In-memory MaxMind GeoIP2 Reader wrapper (zero-disk RAM lookups).

The country database is opened once in ``MODE_MEMORY`` so every lookup
after startup is served from RAM — no per-request disk I/O. Missing DB,
private/reserved IPs, and lookup errors all map to ``"XX"`` (unknown)
instead of raising, so tracking never breaks the pixel response.
"""

from __future__ import annotations

import ipaddress
import logging
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

UNKNOWN = "XX"


class GeoLookup:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._reader = None
        if self.db_path.is_file():
            try:
                import geoip2.database
                import maxminddb

                self._reader = geoip2.database.Reader(str(self.db_path), mode=maxminddb.MODE_MEMORY)
                log.info("GeoIP2 loaded into RAM from %s", self.db_path)
            except Exception as exc:  # noqa: BLE001 — tracking must never break on GeoIP failure
                log.warning("GeoIP2 init failed (%s): country resolution disabled", exc)
        else:
            log.warning("GeoIP2 DB not found at %s: country resolution disabled", self.db_path)

    def country(self, ip: str) -> str:
        """Return ISO 3166-1 alpha-2 country code, or ``"XX"`` when unknown."""
        try:
            parsed = ipaddress.ip_address(ip.strip())
            if parsed.is_private or parsed.is_loopback or parsed.is_multicast or parsed.is_reserved:
                return UNKNOWN
        except ValueError:
            return UNKNOWN
        if self._reader is None:
            return UNKNOWN
        try:
            resp = self._reader.country(ip.strip())
            code = (resp.country.iso_code or "").upper()
            return code if len(code) == 2 else UNKNOWN
        except Exception:  # noqa: BLE001 — unknown IP/DB state maps to "XX"
            return UNKNOWN

    def close(self) -> None:
        try:
            if self._reader is not None:
                self._reader.close()
        except Exception:  # noqa: BLE001,S110 — close is best-effort
            pass
        self._reader = None


@lru_cache(maxsize=1)
def get_geo() -> GeoLookup:
    from .config import get_settings

    return GeoLookup(get_settings().geoip_db_path)
