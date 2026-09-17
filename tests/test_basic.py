"""Smoke tests for the ingestion engine (SQLite per-test temp file)."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client(tmp_path):
    # Isolate each test to a temp SQLite file (shared in-memory DBs don't survive pools).
    from collector.config import get_settings

    get_settings.cache_clear()
    import os

    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    from collector import database

    await database.close_db()
    await database.init_db()

    from collector.main import _limiter, app

    _limiter.reset()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await database.close_db()
    get_settings.cache_clear()


async def test_pixel_returns_svg_with_no_store(client):
    r = await client.get("/pixel/my-doc.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.headers["Cache-Control"] == "no-store, no-cache, must-revalidate, max-age=0"
    assert r.headers["Pragma"] == "no-cache"
    assert "<svg" in r.text


async def test_pixel_rejects_bad_key(client):
    r = await client.get("/pixel/..%2Fetc.svg")
    assert r.status_code in (400, 404)


async def test_heartbeat_accepts_buckets(client):
    for t in (15, 30, 60, 120):
        r = await client.post("/heartbeat/my-doc", json={"t": t})
        assert r.status_code == 200, r.text
        assert r.json()["t"] == t


async def test_heartbeat_rejects_bad_bucket(client):
    r = await client.post("/heartbeat/my-doc", json={"t": 999})
    assert r.status_code == 422


async def test_embed_has_csp_and_beacon(client):
    r = await client.get("/embed/my-doc")
    assert r.status_code == 200
    csp = r.headers["Content-Security-Policy"]
    # Wildcards never match the apex domain - shared Notion pages embed via it.
    assert "https://notion.so" in csp and "https://*.notion.so" in csp
    assert "sendBeacon" in r.text
    assert "views" in r.text


async def test_stats_aggregates(client):
    from datetime import UTC, datetime

    await client.get("/pixel/readme.svg")
    await client.post("/heartbeat/readme", json={"t": 15})
    data = await _wait_for_views(client, "readme")
    assert data["views"] >= 1
    assert data["dwell"].get("15", 0) >= 1
    # Daily time-series: all test events land "today" (UTC).
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert data["daily"][today]["views"] >= 1
    assert data["daily"][today]["uniques"] >= 1
    assert data["daily"][today]["heartbeats"].get("15", 0) >= 1


def test_salt_hash_is_stable_within_epoch():
    from collector import salt

    s = salt.current_salt()
    assert salt.hash_ip("1.2.3.4", s) == salt.hash_ip("1.2.3.4", s)
    assert salt.hash_ip("1.2.3.4", s) != salt.hash_ip("5.6.7.8", s)


def test_geo_unknown_on_private_ip():
    from collector.geo import GeoLookup

    g = GeoLookup("/nonexistent/GeoLite2-Country.mmdb")
    assert g.country("127.0.0.1") == "XX"
    assert g.country("not-an-ip") == "XX"


async def _wait_for_views(client, doc_key: str, minimum: int = 1) -> dict:
    """Pixel ingestion runs as a background task - poll until it lands."""
    data: dict = {}
    for _ in range(20):
        r = await client.get(f"/stats/{doc_key}")
        assert r.status_code == 200
        data = r.json()
        if data["views"] >= minimum:
            break
        await asyncio.sleep(0.1)
    return data


async def test_referrer_stored_origin_only(client):
    await client.get(
        "/pixel/ref-test.svg",
        headers={"referer": "https://mail.example.com/read?token=SECRET123&user=bob"},
    )
    data = await _wait_for_views(client, "ref-test")
    refs = [r["referrer"] for r in data["referrers"]]
    assert refs == ["https://mail.example.com"]
    assert "SECRET123" not in str(data)


async def test_referrer_strips_userinfo(client):
    await client.get(
        "/pixel/creds.svg",
        headers={"referer": "https://user:s3cret@mail.example.com/inbox"},
    )
    data = await _wait_for_views(client, "creds")
    refs = [r["referrer"] for r in data["referrers"]]
    assert refs == ["https://mail.example.com"]
    assert "s3cret" not in str(data)


async def test_ref_param_accepts_approved_tag(client):
    await client.get("/pixel/tag-test.svg?ref=substack")
    data = await _wait_for_views(client, "tag-test")
    refs = [r["referrer"] for r in data["referrers"]]
    assert refs == ["substack"]


async def test_ref_param_rejects_injection(client):
    await client.get("/pixel/bad-ref.svg", params={"ref": "<script>alert(1)</script>"})
    data = await _wait_for_views(client, "bad-ref")
    assert data["referrers"] == []  # not a tag, not a URL → dropped


async def test_stats_device_breakdown(client):
    # One desktop Chrome, one iPhone Safari → two buckets, desktop wins ties by count.
    await client.get("/pixel/dev.svg", headers={"user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"})
    await client.get("/pixel/dev.svg", headers={"user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"})
    await client.get("/pixel/dev.svg", headers={"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"})
    data = await _wait_for_views(client, "dev", minimum=3)
    devices = {d["device"]: d["count"] for d in data["devices"]}
    assert devices == {"desktop-chrome": 2, "mobile-safari": 1}
    assert len(data["devices"]) == 2


async def test_stats_error_does_not_leak_exception(client, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    from collector import database

    async def boom(_doc):
        raise SQLAlchemyError("host=db.internal user=secret")

    monkeypatch.setattr(database, "get_stats", boom)
    r = await client.get("/stats/some-doc")
    assert r.status_code == 500
    assert "secret" not in r.text
    assert "OperationalError" not in r.text


async def test_rate_limit_blocks_flood(client):
    # Pixels silently drop over-limit ingests (an <img> can't render a 429);
    # heartbeats return 429. Keyed on the socket IP, so spoofed XFF headers
    # cannot buy new buckets - even with TRUST_PROXY enabled.
    from collector.config import get_settings
    from collector.main import _limiter

    settings = get_settings()
    old_limit, old_trust = _limiter.limit, settings.trust_proxy
    _limiter.limit, settings.trust_proxy = 3, True
    try:
        codes = [
            (
                await client.post(
                    "/heartbeat/flood-doc",
                    json={"t": 15},
                    headers={"X-Forwarded-For": f"10.{i}.0.{i}"},
                )
            ).status_code
            for i in range(6)
        ]
    finally:
        _limiter.limit, settings.trust_proxy = old_limit, old_trust
    assert codes[:3] == [200, 200, 200]
    assert codes.count(429) == 3


def test_rate_limiter_rejects_when_full_and_keeps_existing_keys():
    # A flood of new IPs must not evict live buckets (that would reset limits),
    # so once full, untracked keys are refused and the first client keeps its budget.
    from collector.main import RateLimiter

    limiter = RateLimiter(limit=2, max_ips=3)
    assert [limiter.allow("a"), limiter.allow("a")] == [True, True]
    assert [limiter.allow("b"), limiter.allow("c")] == [True, True]
    assert limiter.allow("d") is False  # full: brand-new key rejected, not evicted in
    assert limiter.allow("a") is False  # existing key still enforces its own limit
    assert limiter.allow("a") is False


def test_rate_limiter_respects_limit_zero_and_reset():
    from collector.main import RateLimiter

    unlimited = RateLimiter(limit=0, max_ips=3)
    assert all(unlimited.allow(str(i)) for i in range(10))
    limiter = RateLimiter(limit=1, max_ips=3)
    assert limiter.allow("x") and not limiter.allow("x")
    limiter.reset()
    assert limiter.allow("x")


async def test_heartbeat_ignores_unknown_fields(client):
    r = await client.post("/heartbeat/small-doc", json={"t": 60, "extra": "x"})
    assert r.status_code == 200  # pydantic ignores unknown fields; nothing retained


async def test_heartbeat_rejects_oversize_body(client):
    r = await client.post("/heartbeat/small-doc", json={"t": 60, "pad": "x" * 100_000})
    assert r.status_code == 413
    assert r.headers["Cache-Control"].startswith("no-store")


async def test_422_carries_no_store(client):
    r = await client.post("/heartbeat/my-doc", json={"t": 999})
    assert r.status_code == 422
    assert r.headers["Cache-Control"].startswith("no-store")


@pytest.mark.parametrize("header", ["DNT", "Sec-GPC"])
async def test_privacy_signal_skips_ingestion(client, monkeypatch, header):
    from collector import database, main

    def unexpected_identity(_request):
        pytest.fail("Opted-out requests must not resolve an identity")

    monkeypatch.setattr(main, "_identity", unexpected_identity)
    headers = {header: "1"}
    assert (await client.get("/pixel/private.svg", headers=headers)).status_code == 200
    assert (await client.get("/embed/private", headers=headers)).status_code == 200
    response = await client.post("/heartbeat/private", json={"t": 15}, headers=headers)
    assert response.json() == {"ok": True, "tracked": False}
    assert (await database.get_stats("private"))["events"] == 0
    assert main._limiter._hits == {}


async def test_stats_token_gate(client, monkeypatch):
    from collector.config import get_settings

    monkeypatch.setenv("STATS_TOKEN", "shh")
    get_settings.cache_clear()
    try:
        assert (await client.get("/stats/my-doc")).status_code == 403
        assert (await client.get("/stats/my-doc?token=wrong")).status_code == 403
        assert (await client.get("/stats/my-doc?token=shh")).status_code == 200
    finally:
        get_settings.cache_clear()


async def test_embed_default_csp_matches_notion_defaults(client):
    from collector.config import DEFAULT_EMBED_ORIGINS

    r = await client.get("/embed/my-doc")
    csp = r.headers["Content-Security-Policy"]
    # Baseline source policy plus the default frame-ancestors list.
    assert "default-src 'none'" in csp
    assert "connect-src 'self'" in csp and "form-action 'none'" in csp
    assert "frame-ancestors " + " ".join(DEFAULT_EMBED_ORIGINS) + ";" in csp


async def test_embed_custom_origins_replace_csp(client, monkeypatch):
    from collector.config import get_settings

    monkeypatch.setenv("EMBED_ALLOWED_ORIGINS", "https://docs.example.com, https://*.team.example.com")
    get_settings.cache_clear()
    try:
        r = await client.get("/embed/my-doc")
        csp = r.headers["Content-Security-Policy"]
        assert "frame-ancestors https://docs.example.com https://*.team.example.com;" in csp
        assert "https://notion.so" not in csp  # custom list replaces defaults
        # Baseline source policy stays locked down regardless of ancestors.
        assert "img-src 'self' data:; connect-src 'self'" in csp
    finally:
        get_settings.cache_clear()


async def test_embed_empty_origins_block_all_framing(client, monkeypatch):
    from collector.config import get_settings

    monkeypatch.setenv("EMBED_ALLOWED_ORIGINS", "")
    get_settings.cache_clear()
    try:
        r = await client.get("/embed/my-doc")
        csp = r.headers["Content-Security-Policy"]
        assert "frame-ancestors 'none';" in csp
        assert "default-src 'none'" in csp  # baseline stays with framing blocked
    finally:
        get_settings.cache_clear()


async def test_privacy_page_carries_locked_csp(client):
    r = await client.get("/privacy")
    assert r.status_code == 200
    csp = r.headers["Content-Security-Policy"]
    assert csp == "default-src 'none'; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'"


async def test_responses_carry_referrer_policy(client):
    for path in ("/badge/ref-pol.svg", "/stats/ref-pol", "/embed/ref-pol", "/privacy"):
        r = await client.get(path)
        assert r.status_code in (200, 400)
        assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_embed_rejects_invalid_origins(monkeypatch):
    from collector.config import Settings

    for bad in ("http://example.com", "https://*example.com", "example.com", "https://a.com/path"):
        with pytest.raises(ValueError):
            Settings(embed_allowed_origins=[bad])


def test_embed_dedupes_origins():
    from collector.config import Settings

    s = Settings(
        embed_allowed_origins=["https://example.com", "https://example.com", "https://other.com"]
    )
    assert s.embed_allowed_origins == ["https://example.com", "https://other.com"]


async def test_retention_deletes_old_events_only(client):
    from datetime import UTC, datetime, timedelta

    from collector import database

    await database.log_event(doc_key="old-doc", ip_hash="a" * 32)
    await database.log_event(doc_key="new-doc", ip_hash="b" * 32)
    async with database.get_engine().begin() as conn:
        old_cutoff = datetime.now(UTC) - timedelta(days=200)
        await conn.execute(
            database.Event.__table__.update().where(database.Event.doc_key == "old-doc")
            .values(ts=old_cutoff)
        )
    deleted = await database.delete_old_events(180)
    assert deleted == 1
    stats = await database.get_stats("new-doc")
    assert stats["events"] == 1
    assert (await database.get_stats("old-doc"))["events"] == 0


async def test_retention_zero_disables(client):
    from collector import database

    await database.log_event(doc_key="keep-doc", ip_hash="c" * 32)
    assert await database.delete_old_events(0) == 0
    assert (await database.get_stats("keep-doc"))["events"] == 1


async def test_purge_doc_erases_all_events_for_key(client):
    from collector import database

    await database.log_event(doc_key="erase-me", ip_hash="d" * 32)
    await database.log_event(doc_key="erase-me", ip_hash="e" * 32, kind="heartbeat", dwell_seconds=15)
    await database.log_event(doc_key="keep-me", ip_hash="f" * 32)
    deleted = await database.purge_doc("erase-me")
    assert deleted == 2
    assert (await database.get_stats("erase-me"))["events"] == 0
    assert (await database.get_stats("keep-me"))["events"] == 1


def test_retention_days_rejects_negative(monkeypatch):
    from collector.config import Settings

    with pytest.raises(ValueError):
        Settings(retention_days=-1)
