# statless-pages

**Know who reads what — without cookies, fingerprinting, or a JS SDK.**

Statless Pages is a self-hostable, **cookie-free analytics** tracker for **Notion pages, Substack newsletters, and GitHub README files**. Paste one snippet — an invisible pixel, an embed widget, or an SVG badge — and get live view counts, referrers, and **reading-depth (dwell-time)** stats from a single small container. No cookies, no fingerprinting, no third-party JS SDK.

- **One command to run** — `docker compose up -d --build`; SQLite included, PostgreSQL optional
- **Privacy by construction** — no `Set-Cookie`, no ETags, no fingerprinting; IPs are HMAC-SHA256-hashed with an in-memory salt that auto-rotates every 24h, so raw IPs are never stored
- **Drop-in snippets** — embed widget for Notion, invisible pixel for newsletters, SVG badge for GitHub
- **Reading-depth stats** — dwell buckets at 15/30/60/120 s show how far readers actually get
- **One JSON endpoint per doc** — views, uniques, dwell buckets, top countries & referrers
- **Privacy signals honored** — requests with `DNT: 1` or `Sec-GPC: 1` are not recorded at all
- **Bounded retention** — events older than `RETENTION_DAYS` (default 180) are auto-deleted; set `0` to keep everything
- **Self-hosted** — your data never leaves your server. AGPLv3, audit it yourself

AGPLv3 · Python 3.13+ · FastAPI · SQLite (default) / PostgreSQL · GeoIP2 · Jinja2

---

## Track your first page in 60 seconds

**1 · Start the server**

```bash
docker compose up -d --build          # macOS/Windows
# Linux: run as your uid so ./data stays writable by the non-root container
STATLESS_UID=$(id -u) STATLESS_GID=$(id -g) docker compose up -d --build
# → http://localhost:8000
```

**2 · Pick a `doc_key` per page** — any slug like `q3-roadmap`, `launch-post`, or `my-repo-readme` (`^[A-Za-z0-9_-]{1,64}$`).

**3 · Paste the snippet for your platform**

### Notion — `/embed` counter + dwell time

1. In Notion, type `/embed`, paste:
   ```
   http://localhost:8000/embed/q3-roadmap
   ```
2. The widget shows a live `● N views` badge and fires `navigator.sendBeacon` heartbeats at 15s / 30s / 60s / 120s. Light/dark mode follows the OS.

### Substack / any newsletter — invisible pixel

```html
<img src="https://YOUR-HOST/pixel/launch-post.svg" width="1" height="1" alt="" />
```

Cache-busting headers (`no-store, no-cache, must-revalidate, max-age=0`) force re-fetch on every open, so repeat reads count correctly.

### GitHub README — SVG badge

```markdown
![views](https://YOUR-HOST/badge/my-repo-readme.svg)
<img src="https://YOUR-HOST/pixel/my-repo-readme.svg" width="1" height="1" alt="" />
```

**4 · Watch it count**

```bash
curl http://localhost:8000/stats/q3-roadmap
```

```json
{
  "doc": "q3-roadmap",
  "events": 42,
  "views": 30,
  "uniques": 18,
  "dwell": {"15": 12, "30": 9, "60": 5, "120": 2},
  "countries": [{"country": "US", "count": 14}, {"country": "DE", "count": 7}],
  "referrers": [{"referrer": "https://news.ycombinator.com", "count": 9}]
}
```

---

## Endpoints

| Method | Route | Notes |
|---|---|---|
| `GET` | `/pixel/{doc_key}.svg` | 1×1 transparent SVG. `Cache-Control: no-store …`, `Pragma: no-cache`, `Expires: 0`, `X-Robots-Tag: noindex, nofollow` |
| `GET` | `/embed/{doc_key}` | ~2.8KB HTML for Notion embeds. `CSP: frame-ancestors https://notion.so https://*.notion.so https://notion.site https://*.notion.site;` + heartbeat JS |
| `POST` | `/heartbeat/{doc_key}` | JSON `{"t": 15\|30\|60\|120}` via `navigator.sendBeacon`. Bodies > 4 KB get `413`; `429` past the rate limit |
| `GET` | `/badge/{doc_key}.svg` | Counter badge for READMEs |
| `GET` | `/stats/{doc_key}` | JSON: views, uniques, dwell buckets, top countries/referrers. **Public by default** — set `STATS_TOKEN` to require `?token=...` |
| `GET` | `/privacy` | Human-readable privacy notice: exactly what's stored, retention, opt-out, legal position |
| `GET` | `/healthz` | Liveness probe |

`doc_key` must match `^[A-Za-z0-9_-]{1,64}$`.

## Configuration

| Var | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/statless.db` | Use `postgresql+asyncpg://user:pass@db:5432/statless` for Postgres |
| `GEOIP_DB_PATH` | `data/GeoLite2-Country.mmdb` | Country resolution; `XX` when missing |
| `SALT_ROTATE_HOURS` | `24` | IP-hash salt rotation window (must be > 0) |
| `RETENTION_DAYS` | `180` | Auto-delete events older than this. `0` disables automatic deletion (you then own the storage-limitation duty) |
| `BASE_URL` | `http://localhost:8000` | Rendered into embed/badge snippets |
| `TRUST_PROXY` | `false` | Honor `X-Forwarded-For` / `X-Real-IP` for IP **hashing/geo** only. Leave off unless behind a proxy that overwrites these headers — otherwise clients can spoof uniques. The rate limiter always uses the real socket IP, unaffected by this flag. |
| `RATE_LIMIT` | `120` | Tracked events per minute per client (0 disables). Over-limit pixels are silently dropped; heartbeats get `429`. Keyed on the socket IP, so header spoofing can't evade it. |
| `STATS_TOKEN` | *(empty = public)* | When set, `GET /stats` requires `?token=<value>` (returns `403` otherwise). Use your tokenized URL yourself — the embed badge links the plain URL, which 403s for everyone else. |
| `STATLESS_HOST` / `STATLESS_PORT` | `0.0.0.0` / `8000` | Bind for the `statless` entrypoint (namespaced so stray `HOST`/`PORT` env vars can't hijack them) |
| `STATLESS_UID` / `STATLESS_GID` | `10001` | docker-compose only: run the container as your host user so the SQLite bind-mount is writable. On **Linux**: `STATLESS_UID=$(id -u) STATLESS_GID=$(id -g) docker compose up -d` |

## GeoIP2 database

`data/GeoLite2-Country.mmdb` is **not** committed (MaxMind license). Download it free:

1. Create an account at https://www.maxmind.com/en/geolite2/signup
2. Download **GeoLite2 Country** (`.mmdb`), place it at `data/GeoLite2-Country.mmdb`
3. Restart — the DB is loaded once into RAM (`MODE_MEMORY`); lookups never touch disk.

Without the file the service still runs; all countries report as `XX`.

This product includes GeoLite2 data created by MaxMind, available from
https://www.maxmind.com — required attribution notice per the GeoLite2 EULA.

## Local dev (no Docker)

Requires [uv](https://docs.astral.sh/uv/). `uv.lock` is committed so everyone gets the same dependency versions.

```bash
uv sync --extra dev     # creates .venv from uv.lock
uv run pytest -q        # tests
uv run ruff check collector tests
uv run uvicorn collector.main:app --reload
```

## Privacy & compliance

**The short version:** no cookies, no device identifiers, no fingerprinting, no persistent IDs — and requests carrying `DNT: 1` / `Sec-GPC: 1` are dropped before anything is recorded. IP hashes rotate every 24h and raw IPs are never stored; retention is bounded by default (180 days). The full notice is served at `/privacy`.

**The honest version:** cookie-free ≠ law-free.

- **GDPR/ePrivacy (EU):** Pseudonymised IP hashes are still personal data (EDPB Guidelines 01/2025), so processing needs a legal basis. Cookie-free analytics with rotating pseudonymised IPs is commonly run on **legitimate interests** (Art. 6(1)(f)); ePrivacy Art. 5(3) consent questions depend on national interpretations (DE/FR are strictest) and are operator decisions. Run a documented legitimate-interest assessment / DPIA, publish the `/privacy` page, name a contact.
- **UK GDPR / PECR:** mirrors the EU analysis; PECR's "terminal equipment" scope is read similarly to ePrivacy. Operator decision again.
- **US:** no consent banner is typically required for cookie-free, non-identifying analytics, but state laws (CCPA/CPRA, VCDPA, CPA…) differ on "sale/share," "personal information," and universal opt-out signals — GPC is honored here, which helps. Operator decision.

Compliance depends on how *you* deploy and document it — this software provides the technical measures, not a legal guarantee.

## Erasure & retention operations

- **Automatic:** events older than `RETENTION_DAYS` (default 180) are deleted daily.
- **Per-doc erasure:** call `await db.purge_doc("doc-key")` to hard-delete every event for one doc (GDPR Art. 17 helper).
- **Individual erasure limits:** with a rotating salt and no identifiers, the collector generally cannot link stored events back to a person — say so plainly in your privacy notice.

## Scaling notes

**Run a single worker.** The shipped `statless` entrypoint uses one process, and that's deliberate:

- The IP-hash salt and the rate limiter live in process memory, so multiple workers would multiply uniques and rate limits independently (N workers ≈ N× uniques, N× rate limit).
- SQLite is single-writer — extra workers only contend on the write lock.

One event loop easily serves thousands of pixel/heartbeat requests per second; the DB is the bottleneck, not Python.

**If you outgrow it:** move to PostgreSQL, run N replicas, and delegate rate limiting to your proxy (e.g. nginx `limit_req`). Hashing across workers then requires a deterministic, date-based salt derived from a managed `SERVER_SECRET` — planned but not implemented; the current in-memory salt is single-process by design.

## License

AGPLv3 — see [LICENSE](LICENSE).
