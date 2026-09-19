"""Async SQLite/PostgreSQL connection (SQLAlchemy 2.0 asyncio).

Schema - single ``events`` table keeps the standalone story simple:

    id            INTEGER PK
    doc_key       TEXT indexed      (your Notion / Substack / README slug)
    ts            TIMESTAMPTZ       (UTC)
    ip_hash       TEXT              (HMAC-SHA256 w/ rotating salt - never a raw IP)
    country       CHAR(2)           (GeoIP2 ISO code, "XX" unknown)
    referrer      TEXT              (approved tag, or URL origin-only - never a query string)
    device        TEXT              (coarse device label - raw UA discarded for Art. 5(1)(c))
    kind          TEXT              ("view" | "heartbeat")
    dwell_seconds INT nullable      (15/30/60/120 for heartbeats, NULL for views)

SQLite is the default (zero-config single file). Point ``DATABASE_URL`` at
``postgresql+asyncpg://...`` and the same code runs against Postgres.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import (
    ColumnElement,
    CursorResult,
    DateTime,
    Index,
    Integer,
    String,
    and_,
    case,
    delete,
    distinct,
    func,
    select,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_doc_ts", "doc_key", "ts"),
        Index("ix_events_doc_kind", "doc_key", "kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_key: Mapped[str] = mapped_column(String(64), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ip_hash: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="XX")
    referrer: Mapped[str] = mapped_column(String(2048), nullable=False, default="")
    device: Mapped[str] = mapped_column(String(32), nullable=False, default="other")
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="view")
    dwell_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


@asynccontextmanager
async def _session() -> AsyncGenerator[AsyncSession]:
    """Yield a session, or raise if init_db() has not run (no assert - survives -O)."""
    if _session_factory is None:
        raise RuntimeError("call init_db() first")
    async with _session_factory() as session:
        yield session


def get_engine() -> AsyncEngine:
    """Process-wide engine, initialized once from settings. Call init_db() first."""
    global _engine, _session_factory
    if _engine is None:
        from .config import get_settings

        url = get_settings().database_url
        kwargs: dict[str, Any] = {}
        if url.startswith("sqlite"):
            kwargs = {"connect_args": {"check_same_thread": False}}
        _engine = create_async_engine(url, pool_pre_ping=True, **kwargs)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def init_db() -> None:
    """Create tables (and the SQLite directory) from the configured DATABASE_URL."""
    from pathlib import Path

    from .config import get_settings

    url = get_settings().database_url
    # Ensure the SQLite directory exists before connect.
    if url.startswith("sqlite"):
        path = url.split("sqlite+aiosqlite:///", 1)[-1].split("?")[0]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def delete_old_events(retention_days: int) -> int:
    """Delete events older than `retention_days`; return the number deleted.

    GDPR storage-limitation (Art. 5(1)(e)): with RETENTION_DAYS > 0 (default
    180) the lifespan loop prunes daily. 0 disables automatic deletion -
    you then own the storage-limitation duty yourself.
    """
    if retention_days <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    async with _session() as session:
        result = cast(
            "CursorResult[Any]", await session.execute(delete(Event).where(Event.ts < cutoff))
        )
        await session.commit()
        return int(result.rowcount or 0)


async def purge_doc(doc_key: str) -> int:
    """Hard-delete all events for one doc_key (operator maintenance and doc lifecycle purge)."""
    async with _session() as session:
        result = cast(
            "CursorResult[Any]",
            await session.execute(delete(Event).where(Event.doc_key == doc_key)),
        )
        await session.commit()
        return int(result.rowcount or 0)


def _truncate(value: str, limit: int) -> str:
    return value[:limit] if len(value) > limit else value


_REFERRER_MAX = 2048
_UA_MAX = 512
_TAG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Group 1: scheme:// - group 2: host. An optional userinfo@ segment is matched
# but never captured, so credentials in Referer headers are never stored.
_ORIGIN_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)(?:[^@/?#\\]+@)?([^/?#\\]+)")


def _normalize_referrer(value: str) -> str:
    """Keep approved tags as-is; reduce URLs to scheme://host (no userinfo, no query)."""
    value = value.strip()[:_REFERRER_MAX]
    if _TAG_RE.match(value):
        return value
    m = _ORIGIN_RE.match(value)
    return (m.group(1) + m.group(2)) if m else ""


# Coarse UA-family labels (not fingerprints). Order matters: Chromium UAs
# embed "Safari", so mobile OS tokens first, then Edge/Chrome/Firefox/Safari.
_DEVICE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("mobile-safari", ("iphone",)),
    ("mobile-safari", ("ipad",)),
    ("mobile-safari", ("ipod",)),
    ("mobile-chrome", ("android",)),
    ("mobile-safari", ("mobile/",)),
    ("mobile-chrome", ("mobile", "chrome/")),
    ("desktop-edge", ("edg/",)),
    ("desktop-chrome", ("chrome/",)),
    ("desktop-firefox", ("firefox/",)),
    ("desktop-safari", ("safari/",)),
    ("desktop-other", ("linux", "windows", "macintosh")),
)


def classify_device(ua: str) -> str:
    """Map a User-Agent to a coarse family label (mobile/desktop x browser family)."""
    lowered = (ua or "").lower()
    for label, tokens in _DEVICE_RULES:
        if all(token in lowered for token in tokens):
            return label
    return "other"


async def log_event(
    doc_key: str,
    ip_hash: str,
    country: str = "XX",
    referrer: str = "",
    ua: str = "",
    device: str = "",
    kind: str = "view",
    dwell_seconds: int | None = None,
    dedupe_minutes: int = 0,
) -> None:
    """Insert one tracked event; optionally skip duplicate recent views.

    dedupe_minutes > 0 collapses repeated views of the same doc by the same
    ip_hash within the window (rapid double-loads, prefetches) into one.
    The raw User-Agent header is classified at ingest into a coarse device label
    and immediately discarded, satisfying GDPR Art. 5(1)(c) data minimisation.
    """
    dev_label = device or classify_device(ua)
    async with _session() as session:
        if dedupe_minutes > 0:
            cutoff = datetime.now(UTC) - timedelta(minutes=dedupe_minutes)
            shared = (
                await session.execute(
                    select(func.count())
                    .select_from(Event)
                    .where(
                        Event.doc_key == doc_key,
                        Event.ip_hash == _truncate(ip_hash, 32),
                        Event.kind == "view",
                        Event.ts > cutoff,
                    )
                )
            ).scalar()
            if shared:
                return
        session.add(
            Event(
                doc_key=doc_key,
                ts=datetime.now(UTC),
                ip_hash=_truncate(ip_hash, 32),
                country=(country or "XX")[:2].upper() or "XX",
                referrer=_normalize_referrer(referrer or ""),
                device=_truncate(dev_label or "other", 32),
                kind=kind,
                dwell_seconds=dwell_seconds,
            )
        )
        await session.commit()


async def get_view_count(doc_key: str) -> int:
    """Lightweight single-query count for the embed badge."""
    async with _session() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(Event)
                .where(Event.doc_key == doc_key, Event.kind == "view")
            )
        ).scalar() or 0


async def count_events(doc_key: str) -> int:
    """Total tracked events for one doc (test/debug helper)."""
    async with _session() as session:
        return (
            await session.execute(
                select(func.count()).select_from(Event).where(Event.doc_key == doc_key)
            )
        ).scalar() or 0


async def iter_export(doc_key: str, include_identity: bool = True) -> AsyncIterator[dict[str, Any]]:
    """Raw event rows for one doc, oldest first, streamed (NDJSON export).

    Streaming keeps memory flat no matter how many events a doc has.
    ``include_identity=False`` omits the pseudonymous ``ip_hash`` and ``device``
    columns - used when stats are public, so the per-visitor pseudonym trail is
    only published to token holders. Note: this provides operator document backup
    and event audit export; it does not serve as an individual GDPR Art. 15/20
    data subject export because individual visitors are non-identifiable under Art. 11(2).
    """
    fields = ["ts", "doc_key", "country", "referrer", "kind", "dwell_seconds"]
    columns = [
        Event.ts,
        Event.doc_key,
        Event.country,
        Event.referrer,
        Event.kind,
        Event.dwell_seconds,
    ]
    if include_identity:
        fields[2:2] = ["ip_hash", "device"]
        columns[2:2] = [Event.ip_hash, Event.device]
    async with _session() as session:
        result = await session.stream(
            select(*columns).where(Event.doc_key == doc_key).order_by(Event.ts)
        )
        async for row in result:
            out: dict[str, Any] = {}
            for name, value in zip(fields, row, strict=True):
                if name == "ts":
                    out["ts"] = value.isoformat() if hasattr(value, "isoformat") else str(value)
                else:
                    out[name] = value
            yield out


def _like_prefix(prefix: str) -> str:
    """Escape LIKE metacharacters so a doc-key prefix matches literally."""
    return prefix.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _utc_day_expr() -> ColumnElement[Any]:
    """SQL expression for the UTC calendar day of ``Event.ts``.

    SQLite stores UTC ISO strings, so ``date(ts)`` is already UTC. On Postgres,
    ``date(timestamptz)`` converts in the session timezone - force UTC with
    ``AT TIME ZONE 'UTC'`` so daily buckets never drift with the server TZ.
    """
    engine = get_engine()
    if engine.dialect.name == "postgresql":
        return func.date(func.timezone("UTC", Event.ts))
    return func.date(Event.ts)


async def get_overview(prefix: str | None = None) -> list[dict[str, Any]]:
    """Per-doc totals across the whole site, busiest first (optional doc-key prefix)."""
    views = func.sum(case((Event.kind == "view", 1), else_=0)).label("views")
    stmt = (
        select(
            Event.doc_key,
            func.count().label("events"),
            views,
            func.count(distinct(Event.ip_hash)).label("uniques"),
            func.max(Event.ts).label("last_ts"),
        )
        .group_by(Event.doc_key)
        .order_by(views.desc(), Event.doc_key)
    )
    if prefix:
        stmt = stmt.where(Event.doc_key.like(_like_prefix(prefix) + "%", escape="\\"))
    async with _session() as session:
        rows = (await session.execute(stmt)).all()
    return [
        {
            "doc": doc,
            "events": int(events or 0),
            "views": int(v or 0),
            "uniques": int(uniques or 0),
            "last_ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
        }
        for doc, events, v, uniques, ts in rows
    ]


async def get_stats(
    doc_key: str,
    since: str | None = None,
    to: str | None = None,
) -> dict[str, Any]:
    """Aggregate counts for a doc: totals, dwell, daily time-series, top countries/referrers.

    since/to are inclusive UTC date bounds (YYYY-MM-DD), validated by the route.
    One conditional-aggregation query for totals, then four GROUP BYs.
    """
    conditions = [Event.doc_key == doc_key]
    if since:
        conditions.append(Event.ts >= datetime.fromisoformat(since).replace(tzinfo=UTC))
    if to:
        upper = datetime.fromisoformat(to).replace(tzinfo=UTC) + timedelta(days=1)
        conditions.append(Event.ts < upper)
    where = and_(*conditions)
    async with _session() as session:
        total, views, uniques = (
            await session.execute(
                select(
                    func.count(),
                    func.sum(case((Event.kind == "view", 1), else_=0)),
                    func.count(distinct(Event.ip_hash)),
                ).where(where)
            )
        ).one()
        heartbeats = (
            await session.execute(
                select(Event.dwell_seconds, func.count())
                .where(where, Event.kind == "heartbeat")
                .group_by(Event.dwell_seconds)
            )
        ).all()
        countries = (
            await session.execute(
                select(Event.country, func.count())
                .where(where)
                .group_by(Event.country)
                .order_by(func.count().desc())
                .limit(10)
            )
        ).all()
        referrers = (
            await session.execute(
                select(Event.referrer, func.count())
                .where(where, Event.referrer != "")
                .group_by(Event.referrer)
                .order_by(func.count().desc())
                .limit(10)
            )
        ).all()
        day = _utc_day_expr()
        daily_rows = (
            await session.execute(
                select(
                    day.label("day"),
                    Event.kind,
                    Event.dwell_seconds,
                    func.count().label("n"),
                    func.count(distinct(Event.ip_hash)).label("uniq"),
                )
                .where(where)
                .group_by(day, Event.kind, Event.dwell_seconds)
            )
        ).all()

        daily: dict[str, dict[str, Any]] = {}
        for day, kind, dwell, n, uniq in daily_rows:
            day_bucket = daily.setdefault(str(day), {"views": 0, "uniques": 0, "heartbeats": {}})
            if kind == "view":
                day_bucket["views"] += n
                day_bucket["uniques"] = max(day_bucket["uniques"], uniq)
            else:
                day_bucket["heartbeats"][str(dwell or 0)] = n

        devices = (
            await session.execute(
                select(Event.device, func.count())
                .where(where, Event.kind == "view")
                .group_by(Event.device)
                .order_by(func.count().desc(), Event.device)
            )
        ).all()
        device_list = sorted(
            [{"device": dev or "other", "count": n} for dev, n in devices],
            key=lambda item: (-item["count"], item["device"]),
        )

    return {
        "doc": doc_key,
        "events": total,
        "views": int(views or 0),
        "uniques": uniques,
        "dwell": {str(d or 0): c for d, c in heartbeats},
        "daily": daily,
        "countries": [{"country": c, "count": n} for c, n in countries],
        "referrers": [{"referrer": r, "count": n} for r, n in referrers],
        "devices": device_list,
    }
