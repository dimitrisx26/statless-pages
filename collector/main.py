"""FastAPI routes & cache-busting headers.

Endpoints:
    GET  /pixel/{doc_key}.svg   1x1 transparent SVG, never cached
    GET  /embed/{doc_key}        Notion /embed widget (~2.8KB) + 15s heartbeats
    POST /heartbeat/{doc_key}    dwell-time beacons {"t": 15|30|60|120}
    GET  /badge/{doc_key}.svg    SVG view counter (for GitHub READMEs)
    GET  /stats/{doc_key}        JSON aggregates
    GET  /healthz                liveness probe
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import database as db
from . import salt
from .config import get_settings
from .geo import get_geo

log = logging.getLogger(__name__)

DOC_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PIXEL_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1">'
    '<rect width="1" height="1" fill="none"/></svg>'
)
# Apex domains matter: *.notion.so never matches https://notion.so itself.
EMBED_CSP = (
    "frame-ancestors https://notion.so https://*.notion.so "
    "https://notion.site https://*.notion.site;"
)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _no_store(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    # Keep crawler traffic (which fetches pixel/badge/embed URLs) out of both
    # search indexes and your view counts.
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


def _valid_doc(doc_key: str) -> bool:
    return bool(DOC_KEY_RE.match(doc_key))


def _tracking_allowed(request: Request) -> bool:
    return not any(request.headers.get(header, "").strip() == "1" for header in ("dnt", "sec-gpc"))


def _socket_ip(request: Request) -> str:
    """Direct TCP peer — spoof-proof (ignores headers), so the rate limiter keys on this."""
    return request.client.host if request.client else "unknown"


def _client_ip(request: Request) -> str:
    """IP for hashing/geo. Honors proxy headers ONLY when TRUST_PROXY=true."""
    settings = get_settings()
    if settings.trust_proxy:
        # Only trustworthy behind a proxy that overwrites (not appends) these headers.
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
        real = request.headers.get("x-real-ip", "")
        if real:
            return real.strip()
    return request.client.host if request.client else ""


def _referrer(request: Request, ref: str = "") -> str:
    """?ref= wins over the Referer header; the db layer normalizes/sanitizes the value."""
    header = request.headers.get("referer", "") or request.headers.get("referrer", "")
    return ref or header or ""


class RateLimiter:
    """Fixed-window counter per client, per minute. Single event loop, no locks."""

    def __init__(self, limit: int, max_ips: int = 10_000) -> None:
        if max_ips < 1:
            raise ValueError("max_ips must be positive")
        self.limit = limit
        self.max_ips = max_ips
        self._hits: dict[str, tuple[int, float]] = {}
        self._last_sweep = float("-inf")

    def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic()
        if key not in self._hits and len(self._hits) >= self.max_ips:
            if now - self._last_sweep >= 60.0:
                self._last_sweep = now
                cutoff = now - 60.0
                self._hits = {k: v for k, v in self._hits.items() if v[1] > cutoff}
            if len(self._hits) >= self.max_ips:
                return False
        count, window_start = self._hits.get(key, (0, now))
        if now - window_start >= 60.0:
            count, window_start = 0, now
        if count >= self.limit:
            return False
        self._hits[key] = (count + 1, window_start)
        return True

    def reset(self) -> None:
        self._hits.clear()
        self._last_sweep = float("-inf")


_limiter = RateLimiter(get_settings().rate_limit)


def _identity(request: Request) -> tuple[str, str]:
    """Resolve (ip_hash, country) once per request.

    When the IP is unresolvable (no client socket), a random hash keeps such
    events from collapsing into one shared "unique".
    """
    ip = _client_ip(request)
    if not ip:
        return salt.hash_ip(f"anon-{time.time_ns()}"), "XX"
    return salt.hash_ip(ip), get_geo().country(ip)


async def _record_view(doc_key: str, request: Request, ref: str = "") -> None:
    ip_hash, country = _identity(request)
    try:
        await db.log_event(
            doc_key=doc_key,
            ip_hash=ip_hash,
            country=country,
            referrer=_referrer(request, ref),
            ua=request.headers.get("user-agent", "") or "",
            kind="view",
        )
    except Exception:
        log.debug("view ingest failed for %s", doc_key, exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    salt.start_rotation_loop()
    get_geo()  # warm the in-RAM reader
    retention = get_settings().retention_days

    async def retention_loop() -> None:
        if retention <= 0:
            return
        while True:
            deleted = await db.delete_old_events(retention)
            if deleted:
                log.info("retention: deleted %d events older than %d days", deleted, retention)
            await asyncio.sleep(86_400)

    task = asyncio.create_task(retention_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await salt.stop_rotation_loop()
    get_geo().close()
    await db.close_db()


app = FastAPI(title="statless-pages", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # beacon POSTs come from Notion iframes; no cookies involved
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class BodyLimitMiddleware:
    """Reject POST/PUT/PATCH bodies over `max_bytes` with 413, before parsing.

    Covers declared Content-Length and chunked bodies (bounded buffer + replay).
    Only /heartbeat accepts a body, and its payloads are < 100 bytes.
    """

    def __init__(self, app, max_bytes: int = 4096) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return
        declared: int | None = None
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = self.max_bytes + 1
                break
        if declared is not None and declared > self.max_bytes:
            await self._reject(scope, receive, send)
            return
        if declared is not None:
            await self.app(scope, receive, send)
            return
        # Unknown length (chunked): buffer bounded, then replay downstream.
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > self.max_bytes:
                await self._reject(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        sent = False

        async def replay():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)

    async def _reject(self, scope, receive, send) -> None:
        resp = _no_store(JSONResponse({"ok": False, "error": "payload too large"}, status_code=413))
        await resp(scope, receive, send)


app.add_middleware(BodyLimitMiddleware)


@app.exception_handler(RequestValidationError)
async def _validation_no_store(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Same shape as FastAPI's default 422, plus no-store. Bodies are capped at
    # 4 KB by BodyLimitMiddleware, so the echoed `input` stays bounded.
    return _no_store(JSONResponse(status_code=422, content=jsonable_encoder(exc.errors())))


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


PRIVACY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>statless-pages · privacy</title>
<style>
body{font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Helvetica,Arial,sans-serif;
 max-width:44em;margin:0 auto;padding:2rem 1rem;color:#37352f;background:#ffffff}
h1{font-size:1.5rem}h2{font-size:1.1rem;margin-top:2em}
code{background:#f1f1ef;padding:0.15em 0.4em;border-radius:4px;font-size:0.9em}
table{border-collapse:collapse;width:100%}
th,td{border:1px solid #e3e2e0;padding:0.4em 0.6em;text-align:left;font-size:0.9em}
</style>
</head>
<body>
<h1>Privacy — what this collector stores (and what it never stores)</h1>
<p>This service counts page views and reading time for Notion pages, newsletters,
and README files. It sets <strong>no cookies</strong>, uses no ETags, and does no
fingerprinting. This page describes the data this collector itself processes;
the operator of this deployment is the controller for it.</p>

<h2>What is stored per event</h2>
<table>
<tr><th>Field</th><th>Content</th><th>Purpose</th></tr>
<tr><td><code>doc_key</code></td><td>The slug you chose (e.g. <code>q3-roadmap</code>)</td><td>Which page was viewed</td></tr>
<tr><td><code>ts</code></td><td>UTC timestamp</td><td>When</td></tr>
<tr><td><code>ip_hash</code></td><td>HMAC-SHA256(IP, salt)[:32], salt RAM-only, rotated every
SALT_ROTATE_HOURS (default 24h). Raw IPs are never written to disk.</td><td>Approximate unique counts</td></tr>
<tr><td><code>country</code></td><td>2-letter ISO country from GeoLite2 (or <code>XX</code> unknown)</td><td>Coarse geography only</td></tr>
<tr><td><code>referrer</code></td><td>Either an approved <code>?ref=</code> tag or <code>scheme://host</code> only —
query strings and userinfo are stripped</td><td>Where readers came from</td></tr>
<tr><td><code>ua</code></td><td>User-Agent, truncated to 512 chars</td><td>Approximate client stats</td></tr>
<tr><td><code>dwell_seconds</code></td><td>One of 15/30/60/120 (heartbeats only)</td><td>Reading depth</td></tr>
</table>

<h2>Never stored</h2>
<p>Raw IP addresses. Cookies or device identifiers. Query strings or paths from
referrers. Scroll, click, or input telemetry. Heartbeat payloads beyond the
4-value bucket <code>{"t": 15|30|60|120}</code>.</p>

<h2>Retention</h2>
<p>Events older than <code>RETENTION_DAYS</code> (default 180) are deleted
automatically. <code>RETENTION_DAYS=0</code> disables automatic deletion — the
operator then carries the storage-limitation duty themselves.</p>

<h2>Opt-out</h2>
<p>Requests carrying <code>DNT: 1</code> or <code>Sec-GPC: 1</code> are not
recorded (nothing is logged for them, not even the visit).</p>

<h2>Erasure</h2>
<p>Because this tracker is cookie-free and stores only salted, rotating IP
hashes, it generally cannot re-identify a person to fulfill an individual
erasure request. Operators can hard-delete all events for a given
<code>doc_key</code> with <code>db.purge_doc()</code>, and daily retention
pruning bounds all stored data automatically.</p>

<h2>Legal position (not legal advice)</h2>
<p>Cookie-free, no-identifier tracking with rotating pseudonymised IPs is
commonly run on legitimate interests (GDPR Art. 6(1)(f)); pseudonymised IP
hashes remain personal data under GDPR (EDPB Guidelines 01/2025). Whether any
particular deployment needs a consent banner depends on the operator's
documented legitimate-interest assessment and on national ePrivacy
interpretations (notably DE/FR). Operators embedding this tracker in EU-facing
pages should publish this page, name a contact, and record a DPIA/legitimate-
interest assessment. UK GDPR/PECR and US state privacy laws (e.g. CCPA/CPRA
"sale/share" and universal opt-out signals like GPC) have separate,
operator-specific requirements — this software implements the technical
signals (DNT/Sec-GPC honoring, no cookies, origin-only referrers, bounded
retention), but compliance decisions rest with the operator.</p>
</body>
</html>"""


@app.get("/privacy", include_in_schema=False)
async def privacy() -> Response:
    return _no_store(HTMLResponse(PRIVACY_HTML))


@app.get("/", include_in_schema=False)
async def index() -> JSONResponse:
    s = get_settings()
    return JSONResponse(
        {
            "service": "statless-pages",
            "privacy": "cookie-free; IPs HMAC-hashed with a daily-rotating salt, never stored",
            "usage": {
                "pixel": f"{s.base_url}/pixel/YOUR-DOC.svg",
                "embed": f"{s.base_url}/embed/YOUR-DOC",
                "badge": f"{s.base_url}/badge/YOUR-DOC.svg",
                "stats": f"{s.base_url}/stats/YOUR-DOC",
            },
        }
    )


@app.get("/pixel/{doc_key}", include_in_schema=False)
@app.get("/pixel/{doc_key}.svg", include_in_schema=False)
async def pixel(doc_key: str, request: Request, background: BackgroundTasks, ref: str = "") -> Response:
    if not _valid_doc(doc_key):
        return _no_store(Response("invalid doc key", status_code=400, media_type="text/plain"))
    # Over-limit views are silently dropped: an <img> can't render a 429.
    if _tracking_allowed(request) and _limiter.allow(_socket_ip(request)):
        background.add_task(_record_view, doc_key, request, ref)
    return _no_store(Response(PIXEL_SVG, media_type="image/svg+xml"))


@app.get("/embed/{doc_key}", response_class=HTMLResponse)
async def embed(doc_key: str, request: Request, background: BackgroundTasks) -> Response:
    if not _valid_doc(doc_key):
        return _no_store(Response("invalid doc key", status_code=400, media_type="text/plain"))
    if _tracking_allowed(request) and _limiter.allow(_socket_ip(request)):
        background.add_task(_record_view, doc_key, request)
    try:
        views: int = await db.get_view_count(doc_key)
    except Exception:  # noqa: BLE001 — badge degrades to 0 on DB errors
        views = 0
    resp = TEMPLATES.TemplateResponse(
        request,
        "embed.html",
        {"doc_key": doc_key, "views": views, "base_url": get_settings().base_url.rstrip("/")},
    )
    resp.headers["Content-Security-Policy"] = EMBED_CSP
    return _no_store(resp)


class Heartbeat(BaseModel):
    t: Literal[15, 30, 60, 120] = Field(description="Dwell bucket in seconds")


@app.post("/heartbeat/{doc_key}")
async def heartbeat(doc_key: str, beat: Heartbeat, request: Request) -> JSONResponse:
    if not _valid_doc(doc_key):
        return _no_store(JSONResponse({"ok": False, "error": "invalid doc key"}, status_code=400))
    if not _tracking_allowed(request):
        return _no_store(JSONResponse({"ok": True, "tracked": False}))
    if not _limiter.allow(_socket_ip(request)):
        return _no_store(JSONResponse({"ok": False, "error": "rate limited"}, status_code=429))
    ip_hash, country = _identity(request)
    try:
        await db.log_event(
            doc_key=doc_key,
            ip_hash=ip_hash,
            country=country,
            referrer=_referrer(request),
            ua=request.headers.get("user-agent", "") or "",
            kind="heartbeat",
            dwell_seconds=beat.t,
        )
    except Exception:
        log.debug("heartbeat ingest failed for %s", doc_key, exc_info=True)
    return _no_store(JSONResponse({"ok": True, "t": beat.t}))


@app.get("/badge/{doc_key}.svg", include_in_schema=False)
async def badge(doc_key: str) -> Response:
    if not _valid_doc(doc_key):
        return _no_store(Response("invalid", status_code=400, media_type="text/plain"))
    try:
        views = await db.get_view_count(doc_key)
    except Exception:  # noqa: BLE001 — badge degrades to 0 on DB errors
        views = 0
    label = f"{views} views" if views != 1 else "1 view"
    w = 8 * len(label) + 28
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="20" role="img" aria-label="{label}">'
        f'<rect width="{w}" height="20" rx="10" fill="#f1f1ef"/>'
        f'<circle cx="10" cy="10" r="3.5" fill="#46a758"/>'
        f'<text x="18" y="14" font-family="Inter,-apple-system,Segoe UI,Helvetica,Arial,sans-serif" '
        f'font-size="11" fill="#37352f">{label}</text></svg>'
    )
    return _no_store(Response(svg, media_type="image/svg+xml"))


@app.get("/stats/{doc_key}")
async def stats(doc_key: str, token: str = "") -> JSONResponse:
    if not _valid_doc(doc_key):
        return _no_store(JSONResponse({"ok": False, "error": "invalid doc key"}, status_code=400))
    expected = get_settings().stats_token
    if expected and not hmac.compare_digest(token, expected):
        # Gated only when STATS_TOKEN is set; default (empty) keeps stats public.
        return _no_store(JSONResponse({"ok": False, "error": "forbidden"}, status_code=403))
    try:
        data = await db.get_stats(doc_key)
    except Exception:
        log.exception("stats failed for %s", doc_key)
        # Return (don't raise) so the 500 still carries no-store; detail logged, never leaked.
        return _no_store(JSONResponse({"ok": False, "error": "stats unavailable"}, status_code=500))
    return _no_store(JSONResponse(data))


def run() -> None:  # `statless` entrypoint
    import uvicorn

    s = get_settings()
    uvicorn.run("collector.main:app", host=s.host, port=s.port)
