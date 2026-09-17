# statless-pages

Self-hostable, **cookie-free** view, referrer, and dwell-time tracker for **Notion documents, Substack newsletters, and GitHub README files.**

AGPLv3 · Python 3.13+ · FastAPI · SQLite (default) / PostgreSQL · GeoIP2 · Jinja2

No cookies. No fingerprinting. No JavaScript SDK. IPs are HMAC-SHA256 hashed with an in-memory salt that auto-rotates every 24h — raw IPs are never stored.

---

## Quickstart (60 seconds)

```bash
docker compose up -d --build          # macOS/Windows
# Linux: run as your uid so ./data stays writable by the non-root container
STATLESS_UID=$(id -u) STATLESS_GID=$(id -g) docker compose up -d --build
# → http://localhost:8000
```

Pick a `doc_key` per page (e.g. `q3-roadmap`, `launch-post`, `my-repo-readme`).

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

---

## Endpoints

| Method | Route | Notes |
|---|---|---|
| `GET` | `/pixel/{doc_key}.svg` | 1×1 transparent SVG. `Cache-Control: no-store …`, `Pragma: no-cache`, `Expires: 0` |
| `GET` | `/embed/{doc_key}` | ~2.8KB HTML for Notion embeds. `CSP: frame-ancestors https://notion.so https://*.notion.so https://notion.site https://*.notion.site;` + heartbeat JS |
| `POST` | `/heartbeat/{doc_key}` | JSON `{"t": 15\|30\|60\|120}` via `navigator.sendBeacon`. Bodies > 4 KB get `413`; `429` past the rate limit |
| `GET` | `/badge/{doc_key}.svg` | Counter badge for READMEs |
| `GET` | `/stats/{doc_key}` | JSON: views, uniques, dwell buckets, top countries/referrers. **Public by default** — set `STATS_TOKEN` to require `?token=...` |
| `GET` | `/healthz` | Liveness probe |

`doc_key` must match `^[A-Za-z0-9_-]{1,64}$`.

## Configuration

| Var | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/statless.db` | Use `postgresql+asyncpg://user:pass@db:5432/statless` for Postgres |
| `GEOIP_DB_PATH` | `data/GeoLite2-Country.mmdb` | Country resolution; `XX` when missing |
| `SALT_ROTATE_HOURS` | `24` | IP-hash salt rotation window (must be > 0) |
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

## Local dev (no Docker)

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install .[dev]
pytest -q
uvicorn collector.main:app --reload
```

## Privacy model

- No `Set-Cookie`, no ETag tracking, no JS fingerprinting.
- IP → `HMAC-SHA256(ip, daily_salt)[:32]`; salt lives only in RAM and rotates daily.
- Country stored as 2-letter code; UA truncated to 512 chars.
- Referrers: `?ref=` tags (`[A-Za-z0-9_-]{1,64}`) kept; URLs reduced to `scheme://host` — query strings (tokens, PII) are never stored.
- Rate-limited per socket IP; `/stats` errors are generic (DB details logged server-side only).
- Heartbeats carry only `{t}` — no scroll/click telemetry.

## Scaling notes

**Run a single worker.** The shipped `statless` entrypoint uses one process, and that's deliberate:

- The IP-hash salt and the rate limiter live in process memory, so multiple workers would multiply uniques and rate limits independently (N workers ≈ N× uniques, N× rate limit).
- SQLite is single-writer — extra workers only contend on the write lock.

One event loop easily serves thousands of pixel/heartbeat requests per second; the DB is the bottleneck, not Python.

**If you outgrow it:** move to PostgreSQL, run N replicas, and delegate rate limiting to your proxy (e.g. nginx `limit_req`). Hashing across workers then requires a deterministic, date-based salt derived from a managed `SERVER_SECRET` — planned but not implemented; the current in-memory salt is single-process by design.

## License

AGPLv3 — see [LICENSE](LICENSE).
