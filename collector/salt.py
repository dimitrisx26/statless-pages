"""In-memory salt state with daily auto-rotation (Python 3.13+).

Privacy model: client IPs are never stored. Each IP is HMAC-SHA256 hashed
with an ephemeral in-memory salt that rotates every ``SALT_ROTATE_HOURS``
(default 24h). After rotation, yesterday's hashes cannot be re-correlated —
uniques are approximate per-salt-window, which is the intended trade-off
for a cookie-free tracker.

The salt lives only in process memory (never on disk, never logged).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets

from .config import get_settings

_salt: str | None = None
_rotation_task: asyncio.Task[None] | None = None


def current_salt() -> str:
    """Return the active salt, generating one lazily on first use."""
    global _salt
    if _salt is None:
        _salt = secrets.token_hex(32)
    return _salt


def rotate_salt() -> str:
    """Force-rotate the salt immediately. Returns the new salt."""
    global _salt
    _salt = secrets.token_hex(32)
    return _salt


def hash_ip(ip: str, salt: str | None = None) -> str:
    """HMAC-SHA256 hash an IP address, truncated to 32 hex chars (128-bit).

    Truncation keeps the SQLite/Postgres index small while remaining
    collision-resistant for per-day unique counting.
    """
    s = salt if salt is not None else current_salt()
    digest = hmac.new(s.encode(), ip.strip().lower().encode(), hashlib.sha256).hexdigest()
    return digest[:32]


async def _rotation_loop() -> None:
    settings = get_settings()
    interval = settings.salt_rotate_hours * 3600.0
    while True:
        await asyncio.sleep(interval)
        rotate_salt()


def start_rotation_loop() -> None:
    """Start the background daily-reset loop (idempotent). Call from lifespan."""
    global _rotation_task
    current_salt()  # ensure a salt exists before serving traffic
    if _rotation_task is None or _rotation_task.done():
        _rotation_task = asyncio.create_task(_rotation_loop())


async def stop_rotation_loop() -> None:
    """Cancel the rotation loop and wait for it to finish. Call from lifespan shutdown."""
    global _rotation_task
    task, _rotation_task = _rotation_task, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
