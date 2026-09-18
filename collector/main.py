"""FastAPI routes & cache-busting headers.

Endpoints:
    GET  /pixel/{doc_key}.svg   1x1 transparent SVG, never cached
    GET  /embed/{doc_key}        Notion /embed widget (~2.8KB) + 15s heartbeats
    POST /heartbeat/{doc_key}    dwell-time beacons {"t": 15|30|60|120}
    GET  /badge/{doc_key}.svg    SVG view counter (for GitHub READMEs)
    GET  /stats/{doc_key}        JSON aggregates for one doc
    GET  /overview               Per-doc totals across the whole site
    GET  /healthz                liveness probe
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from . import __version__, salt
from . import database as db
from .config import get_settings
from .geo import get_geo
from .models import DocStats, OverviewResponse

log = logging.getLogger(__name__)

DOC_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PIXEL_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1">'
    '<rect width="1" height="1" fill="none"/></svg>'
)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _no_store(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return resp


def _valid_doc(doc_key: str) -> bool:
    return bool(DOC_KEY_RE.match(doc_key))


def _invalid_doc_error() -> Response:
    return _no_store(Response("invalid doc key", status_code=400, media_type="text/plain"))


def _tracking_allowed(request: Request) -> bool:
    return not any(request.headers.get(header, "").strip() == "1" for header in ("dnt", "sec-gpc"))


# Crawlers, link-preview/unfurl bots, headless browsers. Deliberately excludes
# generic HTTP clients (curl/httr, python-requests) so manual tests still count.
_BOT_RE = re.compile(
    r"bot|spider|crawl|slurp|archiver|externalhit|embedly|iframely|preview|"
    r"validator|lighthouse|headlesschrome|phantomjs|puppeteer|playwright|"
    r"uptimerobot|pingdom|statuscake|semrush|ahrefs|mj12|dotbot|petal|"
    r"bytespider|gptbot|ccbot|claudebot|perplexity|feedfetcher|dnygr|"
    r"yandex|baidu|duckduck",
    re.IGNORECASE,
)


def _is_bot(request: Request) -> bool:
    return bool(_BOT_RE.search(request.headers.get("user-agent", "") or ""))


def _is_prefetch(request: Request) -> bool:
    """Speculative loads / link previews that were never actually viewed."""
    purpose = " ".join(
        request.headers.get(h, "") for h in ("sec-purpose", "purpose", "x-purpose", "x-moz")
    ).lower()
    return "prefetch" in purpose or "prerender" in purpose or "preview" in purpose


def _counts_as_human(request: Request) -> bool:
    """False for bot/preview traffic when FILTER_BOTS is on (default)."""
    if not get_settings().filter_bots:
        return True
    return not _is_bot(request) and not _is_prefetch(request)


def _socket_ip(request: Request) -> str:
    """Direct TCP peer - spoof-proof (ignores headers), so the rate limiter keys on this."""
    return request.client.host if request.client else "unknown"


def _client_ip(request: Request) -> str:
    """IP for hashing/geo. Honors proxy headers ONLY when TRUST_PROXY=true."""
    settings = get_settings()
    if settings.trust_proxy:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
        real = request.headers.get("x-real-ip", "")
        if real:
            return real.strip()
    return request.client.host if request.client else ""


_UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign")
_UTM_CLEAN_RE = re.compile(r"[^a-z0-9]+")


def _utm_tag(request: Request) -> str:
    """Collapse allow-listed UTM params into one approved tag (e.g. newsletter-launch-2026)."""
    parts: list[str] = []
    for key in _UTM_KEYS:
        cleaned = _UTM_CLEAN_RE.sub("-", request.query_params.get(key, "").strip().lower()).strip("-")
        if cleaned:
            parts.append(cleaned[:24])
    if not parts:
        return ""
    return "-".join(parts)[:64].strip("-")


def _referrer(request: Request, ref: str = "") -> str:
    """?ref= wins, then a UTM tag, then the Referer header; db normalizes the value."""
    if ref:
        return ref
    utm = _utm_tag(request)
    if utm:
        return utm
    return request.headers.get("referer", "") or request.headers.get("referrer", "") or ""


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
    """(ip_hash, country) per request; unresolvable IPs hash as anon so they don't share one bucket."""
    ip = _client_ip(request)
    if not ip:
        return salt.hash_ip(f"anon-{time.time_ns()}"), "XX"
    return salt.hash_ip(ip), get_geo().country(ip)


async def _record_event(
    doc_key: str,
    request: Request,
    kind: str,
    ref: str = "",
    dwell_seconds: int | None = None,
    dedupe_minutes: int = 0,
) -> None:
    """One ingest path for views and heartbeats; failures never propagate."""
    ip_hash, country = _identity(request)
    try:
        await db.log_event(
            doc_key=doc_key,
            ip_hash=ip_hash,
            country=country,
            referrer=_referrer(request, ref),
            ua=request.headers.get("user-agent", "") or "",
            kind=kind,
            dwell_seconds=dwell_seconds,
            dedupe_minutes=dedupe_minutes,
        )
    except Exception:  # ingest must never break pixel responses
        log.debug("%s ingest failed for %s", kind, doc_key, exc_info=True)


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


app = FastAPI(title="statless-pages", version=__version__, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # beacon POSTs come from Notion iframes; no cookies involved
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class BodyLimitMiddleware:
    """Reject POST/PUT/PATCH bodies over `max_bytes` (413) before parsing; chunked-safe."""

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
                except ValueError:  # malformed header: treat as too large
                    declared = self.max_bytes + 1
                break
        if declared is not None and declared > self.max_bytes:
            await self._reject(scope, receive, send)
            return
        if declared is not None:
            await self.app(scope, receive, send)
            return
        # Chunked (no Content-Length): buffer bounded, then replay downstream.
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
    # FastAPI's default 422 shape plus no-store (echoed input is capped at 4 KB).
    return _no_store(JSONResponse(status_code=422, content=jsonable_encoder(exc.errors())))


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.get("/privacy", include_in_schema=False)
async def privacy() -> Response:
    html = (Path(__file__).parent / "templates" / "privacy.html").read_text()
    resp = _no_store(HTMLResponse(html))
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'"
    )
    return resp


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
                "overview": f"{s.base_url}/overview",
            },
        }
    )


@app.get("/pixel/{doc_key}", include_in_schema=False)
@app.get("/pixel/{doc_key}.svg", include_in_schema=False)
async def pixel(doc_key: str, request: Request, background: BackgroundTasks, ref: str = "") -> Response:
    if not _valid_doc(doc_key):
        return _invalid_doc_error()
    # Over-limit views are silently dropped: an <img> can't render a 429.
    if _tracking_allowed(request) and _counts_as_human(request) and _limiter.allow(_socket_ip(request)):
        background.add_task(_record_view, doc_key, request, ref)
    return _no_store(Response(PIXEL_SVG, media_type="image/svg+xml"))


async def _record_view(doc_key: str, request: Request, ref: str = "") -> None:
    await _record_event(
        doc_key, request, "view", ref=ref, dedupe_minutes=get_settings().view_dedupe_minutes
    )


async def _view_count_safe(doc_key: str) -> int:
    """View count, or 0 on DB errors - embed/badge must never 500."""
    try:
        return await db.get_view_count(doc_key)
    except SQLAlchemyError:  # degrade to 0 on DB errors
        return 0


@app.get("/embed/{doc_key}", response_class=HTMLResponse)
async def embed(
    doc_key: str, request: Request, background: BackgroundTasks, theme: str = ""
) -> Response:
    if not _valid_doc(doc_key):
        return _invalid_doc_error()
    if _tracking_allowed(request) and _counts_as_human(request) and _limiter.allow(_socket_ip(request)):
        background.add_task(_record_view, doc_key, request)
    views = await _view_count_safe(doc_key)
    forced = theme if theme in ("light", "dark") else ""  # empty = follow the OS
    resp = TEMPLATES.TemplateResponse(
        request,
        "embed.html",
        {
            "doc_key": doc_key,
            "views": views,
            "base_url": get_settings().base_url.rstrip("/"),
            "theme": forced,
        },
    )
    resp.headers["Content-Security-Policy"] = _embed_csp()
    return _no_store(resp)


def _embed_csp() -> str:
    origins = get_settings().embed_allowed_origins
    ancestors = (
        "frame-ancestors " + " ".join(origins) + ";" if origins else "frame-ancestors 'none';"
    )
    # Self-contained page: nothing legitimately loads from any other origin.
    return (
        "default-src 'none'; "
        "script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; "
        "form-action 'none'; base-uri 'none'; "
        + ancestors
    )


class Heartbeat(BaseModel):
    t: Literal[15, 30, 60, 120] = Field(description="Dwell bucket in seconds")


@app.post("/heartbeat/{doc_key}")
async def heartbeat(doc_key: str, beat: Heartbeat, request: Request) -> JSONResponse:
    if not _valid_doc(doc_key):
        return _no_store(JSONResponse({"ok": False, "error": "invalid doc key"}, status_code=400))
    if not _tracking_allowed(request) or not _counts_as_human(request):
        return _no_store(JSONResponse({"ok": True, "tracked": False}))
    if not _limiter.allow(_socket_ip(request)):
        return _no_store(JSONResponse({"ok": False, "error": "rate limited"}, status_code=429))
    await _record_event(doc_key, request, "heartbeat", dwell_seconds=beat.t)
    return _no_store(JSONResponse({"ok": True, "t": beat.t}))


_COLOR_RE = re.compile(r"^[0-9A-Fa-f]{6}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9 _.-]{1,40}$")


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@app.get("/badge/{doc_key}.svg", include_in_schema=False)
async def badge(doc_key: str, label: str = "", labelColor: str = "", color: str = "") -> Response:
    if not _valid_doc(doc_key):
        return _invalid_doc_error()
    if label and not _LABEL_RE.match(label):
        return _no_store(Response("bad label", status_code=400, media_type="text/plain"))
    for c in (labelColor, color):
        if c and not _COLOR_RE.match(c):
            # SVG attributes are the only output here; ASCII hex keeps it that way.
            return _no_store(Response("bad color", status_code=400, media_type="text/plain"))
    views = await _view_count_safe(doc_key)
    text = label if label else f"{views} views" if views != 1 else "1 view"
    bg = f"#{labelColor}" if labelColor else "#f1f1ef"
    fg = f"#{color}" if color else "#37352f"
    w = 8 * len(text) + 28
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="20" role="img" '
        f'aria-label="{_escape(text)}">'
        f'<rect width="{w}" height="20" rx="10" fill="{bg}"/>'
        f'<circle cx="10" cy="10" r="3.5" fill="#46a758"/>'
        f'<text x="18" y="14" font-family="Inter,-apple-system,Segoe UI,Helvetica,Arial,sans-serif" '
        f'font-size="11" fill="{fg}">{_escape(text)}</text></svg>'
    )
    return _no_store(Response(svg, media_type="image/svg+xml"))


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_date(value: str) -> str | None:
    """Return the value when it is a sane YYYY-MM-DD, else None."""
    if not _DATE_RE.match(value):
        return None
    from datetime import date

    try:
        return value if date.fromisoformat(value) else None
    except ValueError:
        return None


def _validated_dates(since: str, to: str) -> tuple[str, str] | JSONResponse:
    """Shared since/to validation. Returns (since, to) or a 400 JSONResponse."""
    parsed_since, parsed_to = _parse_date(since) or "", _parse_date(to) or ""
    if since and not parsed_since:
        return JSONResponse({"ok": False, "error": "bad since date"}, status_code=400)
    if to and not parsed_to:
        return JSONResponse({"ok": False, "error": "bad to date"}, status_code=400)
    if parsed_since and parsed_to and parsed_since > parsed_to:
        return JSONResponse({"ok": False, "error": "since after to"}, status_code=400)
    return parsed_since, parsed_to


def _stats_authorized(token: str) -> bool:
    """True unless STATS_TOKEN is set and the token does not match (public by default)."""
    expected = get_settings().stats_token
    return not expected or hmac.compare_digest(token, expected)


def _stats_token(request: Request, query_token: str) -> str:
    """Token for stats endpoints. Prefers the header (keeps secrets out of access logs).

    The `?token=` query parameter is kept for backwards compatibility.
    """
    return request.headers.get("x-stats-token", "").strip() or query_token


@app.get("/stats/{doc_key}", response_model=DocStats)
async def stats(
    doc_key: str, request: Request, response: Response, token: str = "", since: str = "", to: str = ""
) -> DocStats:
    if not _valid_doc(doc_key):
        return _no_store(JSONResponse({"ok": False, "error": "invalid doc key"}, status_code=400))
    if not _stats_authorized(_stats_token(request, token)):
        return _no_store(JSONResponse({"ok": False, "error": "forbidden"}, status_code=403))
    dates = _validated_dates(since, to)
    if isinstance(dates, JSONResponse):
        return _no_store(dates)
    parsed_since, parsed_to = dates
    try:
        data = await db.get_stats(doc_key, since=parsed_since or None, to=parsed_to or None)
    except SQLAlchemyError:
        log.exception("stats failed for %s", doc_key)
        return _no_store(JSONResponse({"ok": False, "error": "stats unavailable"}, status_code=500))
    _no_store(response)
    return DocStats(**data)


@app.get("/overview")
async def overview(token: str = "", prefix: str = "") -> JSONResponse:
    """Per-doc totals for the whole site, busiest first. Optional `prefix` scopes to one site."""
    if prefix and not _valid_doc(prefix):
        return _no_store(JSONResponse({"ok": False, "error": "invalid prefix"}, status_code=400))
    if not _stats_authorized(token):
        return _no_store(JSONResponse({"ok": False, "error": "forbidden"}, status_code=403))
    try:
        docs = await db.get_overview(prefix or None)
    except SQLAlchemyError:
        log.exception("overview failed")
        return _no_store(JSONResponse({"ok": False, "error": "overview unavailable"}, status_code=500))
    return _no_store(JSONResponse({"docs": docs, "count": len(docs)}))


@app.get("/export/{doc_key}", include_in_schema=False)
async def export(doc_key: str, request: Request, token: str = "") -> Response:
    if not _valid_doc(doc_key):
        return _no_store(JSONResponse({"ok": False, "error": "invalid doc key"}, status_code=400))
    if not _stats_authorized(_stats_token(request, token)):
        return _no_store(JSONResponse({"ok": False, "error": "forbidden"}, status_code=403))

    async def ndjson() -> AsyncIterator[bytes]:
        # Streams row-by-row so a huge doc cannot buffer the whole table in memory.
        # When stats are public the visitor pseudonym (ip_hash) and the
        # fingerprint-capable ua column are omitted: they are pseudonymous, but
        # still personal data under GDPR - publish them only to token holders.
        include_identity = bool(get_settings().stats_token)
        try:
            async for row in db.iter_export(doc_key, include_identity=include_identity):
                yield (json.dumps(row, separators=(",", ":")) + "\n").encode()
        except SQLAlchemyError:
            log.exception("export failed for %s", doc_key)

    resp = _no_store(StreamingResponse(ndjson(), media_type="application/x-ndjson"))
    resp.headers["Content-Disposition"] = f'attachment; filename="{doc_key}.ndjson"'
    return resp


@app.get("/robots.txt", include_in_schema=False)
async def robots() -> Response:
    body = "User-agent: *\nDisallow: /\n"
    return _no_store(Response(body, media_type="text/plain"))


@app.get("/.well-known/security.txt", include_in_schema=False)
async def security_txt() -> Response:
    body = (
        "Contact: mailto:security@YOUR-DOMAIN.example\n"
        "Preferred-Languages: en\n"
        "Policy: https://YOUR-DOMAIN.example/.well-known/security.txt\n"
    )
    resp = _no_store(Response(body, media_type="text/plain; charset=utf-8"))
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def run() -> None:  # `statless` entrypoint
    import uvicorn

    s = get_settings()
    uvicorn.run("collector.main:app", host=s.host, port=s.port)
