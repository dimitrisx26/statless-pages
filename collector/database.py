"""Async SQLite/PostgreSQL connection (SQLAlchemy 2.0 asyncio).

Schema - single ``events`` table keeps the standalone story simple:

    id            INTEGER PK
    doc_key       TEXT indexed      (your Notion / Substack / README slug)
    ts            TIMESTAMPTZ       (UTC)
    ip_hash       TEXT              (HMAC-SHA256 w/ rotating salt - never a raw IP)
    country       CHAR(2)           (GeoIP2 ISO code, "XX" unknown)
    referrer      TEXT              (approved tag, or URL origin-only - never a query string)
    ua            TEXT              (User-Agent, truncated to 512 chars)
    kind          TEXT              ("view" | "heartbeat")
    dwell_seconds INT nullable      (15/30/60/120 for heartbeats, NULL for views)

SQLite is the default (zero-config single file). Point ``DATABASE_URL`` at
``postgresql+asyncpg://...`` and the same code runs against Postgres.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, Index, Integer, String, case, delete, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
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
    ua: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="view")
    dwell_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker | None = None


def get_engine() -> AsyncEngine:
    """Process-wide engine, initialized once from settings. Call init_db() first."""
    global _engine, _session_factory
    if _engine is None:
        from .config import get_settings

        url = get_settings().database_url
        kwargs: dict = {}
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
    assert _session_factory is not None, "call init_db() first"
    async with _session_factory() as session:
        result = await session.execute(delete(Event).where(Event.ts < cutoff))
        await session.commit()
        return int(result.rowcount or 0)


async def purge_doc(doc_key: str) -> int:
    """Hard-delete all events for one doc_key (Art. 17 erasure helper)."""
    assert _session_factory is not None, "call init_db() first"
    async with _session_factory() as session:
        result = await session.execute(delete(Event).where(Event.doc_key == doc_key))
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
    """Map a User-Agent to a coarse family label (mobile/desktop × browser family)."""
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
    kind: str = "view",
    dwell_seconds: int | None = None,
) -> None:
    assert _session_factory is not None, "call init_db() first"
    async with _session_factory() as session:
        session.add(
            Event(
                doc_key=doc_key,
                ts=datetime.now(UTC),
                ip_hash=_truncate(ip_hash, 32),
                country=(country or "XX")[:2].upper() or "XX",
                referrer=_normalize_referrer(referrer or ""),
                ua=_truncate(ua or "", _UA_MAX),
                kind=kind,
                dwell_seconds=dwell_seconds,
            )
        )
        await session.commit()


async def get_view_count(doc_key: str) -> int:
    """Lightweight single-query count for the embed badge."""
    assert _session_factory is not None, "call init_db() first"
    async with _session_factory() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(Event)
                .where(Event.doc_key == doc_key, Event.kind == "view")
            )
        ).scalar() or 0


async def get_stats(doc_key: str) -> dict:
    """Aggregate counts for a doc: totals, dwell, daily time-series, top countries/referrers.

    One conditional-aggregation query for totals, then four GROUP BYs.
    """
    assert _session_factory is not None, "call init_db() first"
    async with _session_factory() as session:
        total, views, uniques = (
            await session.execute(
                select(
                    func.count(),
                    func.sum(case((Event.kind == "view", 1), else_=0)),
                    func.count(distinct(Event.ip_hash)),
                ).where(Event.doc_key == doc_key)
            )
        ).one()
        heartbeats = (
            await session.execute(
                select(Event.dwell_seconds, func.count())
                .where(Event.doc_key == doc_key, Event.kind == "heartbeat")
                .group_by(Event.dwell_seconds)
            )
        ).all()
        countries = (
            await session.execute(
                select(Event.country, func.count())
                .where(Event.doc_key == doc_key)
                .group_by(Event.country)
                .order_by(func.count().desc())
                .limit(10)
            )
        ).all()
        referrers = (
            await session.execute(
                select(Event.referrer, func.count())
                .where(Event.doc_key == doc_key, Event.referrer != "")
                .group_by(Event.referrer)
                .order_by(func.count().desc())
                .limit(10)
            )
        ).all()
        daily_rows = (
            await session.execute(
                select(
                    func.date(Event.ts).label("day"),
                    Event.kind,
                    Event.dwell_seconds,
                    func.count().label("n"),
                    func.count(distinct(Event.ip_hash)).label("uniq"),
                )
                .where(Event.doc_key == doc_key)
                .group_by(func.date(Event.ts), Event.kind, Event.dwell_seconds)
            )
        ).all()

        daily: dict[str, dict] = {}
        for day, kind, dwell, n, uniq in daily_rows:
            day_bucket = daily.setdefault(str(day), {"views": 0, "uniques": 0, "heartbeats": {}})
            if kind == "view":
                day_bucket["views"] += n
                day_bucket["uniques"] = max(day_bucket["uniques"], uniq)
            else:
                day_bucket["heartbeats"][str(dwell or 0)] = n

        devices = (
            await session.execute(
                select(Event.ua, func.count())
                .where(Event.doc_key == doc_key, Event.kind == "view")
                .group_by(Event.ua)
                .order_by(func.count().desc())
                .limit(200)
            )
        ).all()
        device_counts: dict[str, int] = {}
        for ua, n in devices:
            label = classify_device(ua)
            device_counts[label] = device_counts.get(label, 0) + n
        device_list = sorted(device_counts.items(), key=lambda kv: (-kv[1], kv[0]))

    return {
        "doc": doc_key,
        "events": total,
        "views": int(views or 0),
        "uniques": uniques,
        "dwell": {str(d or 0): c for d, c in heartbeats},
        "daily": daily,
        "countries": [{"country": c, "count": n} for c, n in countries],
        "referrers": [{"referrer": r, "count": n} for r, n in referrers],
        "devices": [{"device": label, "count": n} for label, n in device_list],
    }
