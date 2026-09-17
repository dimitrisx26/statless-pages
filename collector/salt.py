"""In-memory salt state with daily auto-rotation (Python 3.13+).

Privacy model: client IPs are never stored. Each IP is HMAC-SHA256 hashed
with an ephemeral in-memory salt that rotates every ``SALT_ROTATE_HOURS``
(default 24h). After rotation, yesterday's hashes cannot be re-correlated -
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
    """Return the active salt, deriving/generating one lazily on first use."""
    global _salt
    if _salt is None:
        secret = get_settings().server_secret
        _salt = derive_ephemeral_salt(secret) if secret else secrets.token_hex(32)
    return _salt


def derive_ephemeral_salt(server_secret: str) -> str:
    """Deterministic per-rotation-window salt for SERVER_SECRET deployments.

    HMAC(server_secret, "YYYY-MM-DD:window-sequence") reproduces across
    replicas, so uniques survive restarts and remain consistent behind a
    load balancer. The secret never leaves process memory.
    """
    from datetime import UTC, datetime

    window = get_settings().salt_rotate_hours  # config validation keeps this > 0
    now = datetime.now(UTC)
    window_index = int(now.timestamp() // (window * 3600.0))
    message = f"{now:%Y-%m-%d}:{window_index}".encode()
    return hmac.new(server_secret.encode(), message, hashlib.sha256).hexdigest()


def _next_salt() -> None:
    """Advance to the salt for a fresh window (random unless SERVER_SECRET)."""
    global _salt
    secret = get_settings().server_secret
    _salt = derive_ephemeral_salt(secret) if secret else secrets.token_hex(32)


def rotate_salt() -> str:
    """Force-rotate the salt immediately. Returns the new salt."""
    _next_salt()
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
    interval = get_settings().salt_rotate_hours * 3600.0
    while True:
        await asyncio.sleep(interval)
        _next_salt()


def start_rotation_loop() -> None:
    """Start the background salt-rotation loop (idempotent). Call from lifespan."""
    global _rotation_task
    _next_salt()  # ensure a salt exists before serving traffic
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
