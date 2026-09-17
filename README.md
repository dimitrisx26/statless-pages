<div align="center">

# statless-pages

**Self-hosted page-view counts - without cookies, fingerprinting, or a third-party JS SDK.**

<p>
  <a href="https://github.com/dimitrisx26/statless-pages/actions/workflows/ci.yml">
    <img src="https://img.shields.io/github/actions/workflow/status/dimitrisx26/statless-pages/ci.yml?branch=main&amp;style=flat-square&amp;logo=github&amp;label=CI" alt="CI status on main" />
  </a>
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/license-AGPLv3-2563EB?style=flat-square" alt="License: AGPLv3" />
  </a>
</p>

<p>
  <a href="pyproject.toml">
    <img src="https://img.shields.io/badge/Python-3.13%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" alt="Python 3.13 or newer" />
  </a>
  <a href="https://fastapi.tiangolo.com">
    <img src="https://img.shields.io/badge/FastAPI-async-009688?style=flat-square&amp;logo=fastapi&amp;logoColor=white" alt="Built with async FastAPI" />
  </a>
</p>

[Report a bug](https://github.com/dimitrisx26/statless-pages/issues) · [Request a feature](https://github.com/dimitrisx26/statless-pages/issues) · [Privacy notice](#privacy--compliance)

</div>

---

Statless Pages is a self-hosted, **cookie-free analytics** collector. It records requests to an invisible SVG pixel or a Notion embed and exposes counts through a JSON API. An optional SVG badge displays the view count; **the badge does not record views**.

## Why statless

- **Small deployment** - one container with SQLite; PostgreSQL optional
- **No third-party SDK** - pixels need no JavaScript; the embed includes its own heartbeat script
- **Approximate analytics** - views, rotating-IP-hash uniques, countries, referrers, and embed dwell buckets at 15/30/60/120 seconds
- **Privacy signals honored** - requests carrying `DNT: 1` or `Sec-GPC: 1` are not recorded
- **Bounded retention** - events older than `RETENTION_DAYS` (default 180) are auto-deleted
- **Self-hosted storage** - no cookies or fingerprinting; the collector stores salted IP hashes rather than raw IPs

> Dwell buckets measure elapsed time after an embed loads, **not scroll depth or proof of reading**. Image proxies, caching, blocked images, and prefetching affect accuracy. See [platform compatibility](#platform-compatibility).

**Stack:** AGPLv3 · Python 3.13+ · FastAPI · SQLite (default) / PostgreSQL · GeoIP2 · Jinja2

---

## Table of contents

- [Quick start](#quick-start)
- [Platform compatibility](#platform-compatibility)
- [Endpoints](#endpoints)
- [Configuration](#configuration)
- [GeoIP2 database](#geoip2-database)
- [Local development](#local-development)
- [Privacy & compliance](#privacy--compliance)
- [Erasure & retention operations](#erasure--retention-operations)
- [Scaling notes](#scaling-notes)
- [License](#license)

---

## Quick start

### Prerequisites

- **Docker Engine or Docker Desktop**, with Docker Compose v2, for the container setup.
- **Git** to clone the repository.
- **Python 3.13+ and uv** only if you prefer [local development](#local-development) without Docker.
- **A public HTTPS host** when embedding on Notion, GitHub, or another hosted platform. Localhost is for testing on your own machine.

**1 · Clone the repository**

```bash
git clone https://github.com/dimitrisx26/statless-pages.git
cd statless-pages
```

**2 · Start the server**

On macOS or Windows with Docker Desktop:

```bash
docker compose up -d --build
```

On Linux, use your host UID/GID so the non-root container can write to `./data`:

```bash
STATLESS_UID=$(id -u) STATLESS_GID=$(id -g) docker compose up -d --build
```

Check the server at `http://localhost:8000/healthz`. It should return `{"ok":true}`.

**3 · Choose a page key and integration**

Replace `https://YOUR-HOST` in the examples with your collector's public HTTPS address. Set `BASE_URL` to that same address before using the embed.

Pick a `doc_key` per page - any slug like `q3-roadmap`, `launch-post`, or `my-repo-readme` (`^[A-Za-z0-9_-]{1,64}$`) - then use one of the integrations below.

### Notion - embed widget (views + dwell time)

1. In Notion, type `/embed`, paste:
   ```
   https://YOUR-HOST/embed/q3-roadmap
   ```
2. The widget shows a live `● N views` badge and fires `navigator.sendBeacon` heartbeats at 15s / 30s / 60s / 120s. Light/dark mode follows the OS.

> **Note:** the embed HTML carries `CSP: frame-ancestors` restricted to `notion.so` / `notion.site` domains by default, so it will not render inside other iframe-based tools (Coda, Confluence, etc.). Use the pixel or badge below there, or set `EMBED_ALLOWED_ORIGINS` (see [configuration](#configuration)) to allow more origins.

### Custom sites - iframe embed (views + dwell time)

On any site you control (Ghost, WordPress, Hugo, static HTML):

1. Add your site's origin to the allow-list, e.g. `EMBED_ALLOWED_ORIGINS="https://example.com"`.
2. Insert the iframe in your template or page:
   ```html
   <iframe src="https://YOUR-HOST/embed/q3-roadmap" style="width:auto;height:28px;border:0"></iframe>
   ```

The widget renders the live badge and sends dwell heartbeats, exactly like the Notion embed. For platforms that sanitize iframes but allow images, use the pixel instead.

### Substack / any newsletter / any HTML - invisible pixel

```html
<img src="https://YOUR-HOST/pixel/launch-post.svg" width="1" height="1" alt="" />
```

The collector sends cache-prevention headers, but image proxies and email clients may still cache, prefetch, or block requests. Counts represent recorded fetches, not guaranteed human opens. Use this snippet wherever the platform permits remote images or raw HTML.

### GitHub README - badge (display only) + pixel (tracking)

```markdown
![views](https://YOUR-HOST/badge/my-repo-readme.svg)
<img src="https://YOUR-HOST/pixel/my-repo-readme.svg" width="1" height="1" alt="" />
```

The badge is a read-only SVG that displays the current count - it does **not** record views. GitHub proxies README images through `camo.githubusercontent.com`, so referrer and country data will not be meaningful there.

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
  "daily": {
    "2026-09-17": {"views": 12, "uniques": 9, "heartbeats": {"15": 5, "60": 2}},
    "2026-09-18": {"views": 18, "uniques": 11, "heartbeats": {"30": 4}}
  },
  "countries": [{"country": "US", "count": 14}, {"country": "DE", "count": 7}],
  "referrers": [{"referrer": "https://news.ycombinator.com", "count": 9}],
  "devices": [{"device": "desktop-chrome", "count": 16}, {"device": "mobile-safari", "count": 9}]
}
```

---

## Platform compatibility

| Platform | Method | Dwell time | Referrer/Country | Notes |
|---|---|---|---|---|
| **Notion** | `/embed` | Yes (heartbeats) | Partial | Works out of the box (default allow-list covers `notion.so` / `notion.site`) |
| **Custom sites** (Ghost, WordPress, Hugo, static HTML) | `<iframe>` embed or pixel | Yes (embed) | Yes | Set `EMBED_ALLOWED_ORIGINS` to your site's origin, then insert `<iframe src="https://YOUR-HOST/embed/KEY"></iframe>` |
| **Substack, Ghost newsletters** | `<img>` pixel | No | Yes | Requires raw HTML blocks or template editing |
| **Coda** | Pixel/badge; embed unverified | No (pixel) | Origin only (`https://coda.io`) | Coda loads content in its own iframes; adding `https://coda.io` to `EMBED_ALLOWED_ORIGINS` removes the server-side block, but whether Coda accepts generic embeds varies - test first. Pixel always works |
| **Confluence** | Pixel/badge; embed unverified | No (pixel) | Varies | Same: add your Confluence origin to `EMBED_ALLOWED_ORIGINS` and test whether it renders the iframe |
| **GitHub README** | Badge + pixel | No | No (GitHub proxies images via Camo) | Badge renders live counts; pixel records the fetch. Markdown sanitizes `<iframe>`, so embeds are impossible here |
| **GitLab, Codeberg, plain HTML sites** | Badge + pixel | No | Varies | Same as GitHub; check whether the platform proxies images |

**Key constraints, in one place:**

1. **The embed only frames on allow-listed origins.** Default CSP: `frame-ancestors https://notion.so https://*.notion.so https://notion.site https://*.notion.site`. Set `EMBED_ALLOWED_ORIGINS` to frame anywhere you control - heartbeats (dwell buckets) work there too. `EMBED_ALLOWED_ORIGINS=""` blocks all framing.
2. **Pixels require raw HTML.** Platforms that sanitize `<img>` tags (Notion blocks, Slack, some email clients that strip tracking pixels) will not record.
3. **Image proxies strip attribution.** GitHub (Camo), some corporate mail scanners, and privacy tools proxy or block remote images, degrading referrer/country accuracy.
4. **Dwell time needs JavaScript.** Only embeds (which run the heartbeat script) get dwell buckets; pixels record a single view with no dwell data.

---

## Endpoints

| Method | Route | Notes |
|---|---|---|
| `GET` | `/pixel/{doc_key}.svg` | 1×1 transparent SVG. `Cache-Control: no-store …`, `Pragma: no-cache`, `Expires: 0`, `X-Robots-Tag: noindex, nofollow` |
| `GET` | `/embed/{doc_key}` | ~2.8KB HTML embed widget (badge + dwell heartbeats). `CSP: frame-ancestors` allow-list (Notion domains by default; configurable via `EMBED_ALLOWED_ORIGINS`). Works in Notion out of the box; anywhere else you control once allowed |
| `POST` | `/heartbeat/{doc_key}` | JSON `{"t": 15\|30\|60\|120}` via `navigator.sendBeacon`. Bodies > 4 KB get `413`; `429` past the rate limit |
| `GET` | `/badge/{doc_key}.svg` | Counter badge for READMEs. Display-only: does not record views; pair with a pixel if you want fetches counted |
| `GET` | `/stats/{doc_key}` | JSON: views, uniques, daily time-series, dwell buckets, top countries/referrers/devices. **Public by default** - set `STATS_TOKEN` to require `?token=...` |
| `GET` | `/privacy` | Human-readable privacy notice: exactly what's stored, retention, opt-out, legal position |
| `GET` | `/healthz` | Liveness probe |

`doc_key` must match `^[A-Za-z0-9_-]{1,64}$`.

---

## Configuration

For local development, set these variables in the process environment. With Docker Compose, add collector settings to the service's `environment` block in [`docker-compose.yml`](docker-compose.yml), then recreate the container. The supplied Compose file interpolates `BASE_URL` and `SALT_ROTATE_HOURS`; other collector settings are not automatically forwarded from your shell or a Compose `.env` file.

> **Before going public:** use HTTPS, persist and back up `./data`, decide whether statistics should be public, and review proxy trust and retention settings. `STATS_TOKEN` protects the JSON statistics endpoint, not the counts displayed by badges or embeds. Avoid putting secrets in page keys or sharing token-bearing URLs.

| Var | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/statless.db` | Use `postgresql+asyncpg://user:pass@db:5432/statless` for Postgres |
| `GEOIP_DB_PATH` | `data/GeoLite2-Country.mmdb` | Country resolution; `XX` when missing |
| `SALT_ROTATE_HOURS` | `24` | IP-hash salt rotation window (must be > 0) |
| `RETENTION_DAYS` | `180` | Auto-delete events older than this. `0` disables automatic deletion (you then own the storage-limitation duty) |
| `BASE_URL` | `http://localhost:8000` | Rendered into embed/badge snippets |
| `TRUST_PROXY` | `false` | Honor `X-Forwarded-For` / `X-Real-IP` for IP **hashing/geo** only. Leave off unless behind a proxy that overwrites these headers - otherwise clients can spoof uniques. The rate limiter always uses the real socket IP, unaffected by this flag. |
| `RATE_LIMIT` | `120` | Tracked events per minute per client (0 disables). Over-limit pixels are silently dropped; heartbeats get `429`. Keyed on the socket IP, so header spoofing can't evade it. |
| `STATS_TOKEN` | *(empty = public)* | When set, `GET /stats` requires `?token=<value>` (returns `403` otherwise). Use your tokenized URL yourself - the embed badge links the plain URL, which 403s for everyone else. |
| `EMBED_ALLOWED_ORIGINS` | Notion apex + wildcard domains | Comma-separated `https` origins allowed to frame `/embed` (replaces the default list, which also covers the `notion.site` apex/wildcards). A leading `*.` wildcard never matches the apex domain - list both when you need both. `EMBED_ALLOWED_ORIGINS=""` sets `frame-ancestors 'none'` (blocks all framing). |
| `STATLESS_HOST` / `STATLESS_PORT` | `0.0.0.0` / `8000` | Bind for the `statless` entrypoint (namespaced so stray `HOST`/`PORT` env vars can't hijack them) |
| `STATLESS_UID` / `STATLESS_GID` | `10001` | docker-compose only: run the container as your host user so the SQLite bind-mount is writable. On **Linux**: `STATLESS_UID=$(id -u) STATLESS_GID=$(id -g) docker compose up -d` |

---

## GeoIP2 database

`data/GeoLite2-Country.mmdb` is **not** committed (MaxMind license). Download it free:

1. Create an account at https://www.maxmind.com/en/geolite2/signup
2. Download **GeoLite2 Country** (`.mmdb`), place it at `data/GeoLite2-Country.mmdb`
3. Restart - the DB is loaded once into RAM (`MODE_MEMORY`); lookups never touch disk.

Without the file the service still runs; all countries report as `XX`.

> This product includes GeoLite2 data created by MaxMind, available from https://www.maxmind.com - required attribution notice per the GeoLite2 EULA.

---

## Local development

Requires [uv](https://docs.astral.sh/uv/). `uv.lock` is committed so everyone gets the same dependency versions.

```bash
uv sync --extra dev     # creates .venv from uv.lock
uv run pytest -q        # tests
uv run ruff check collector tests
uv run uvicorn collector.main:app --reload
```

---

## Privacy & compliance

**The short version:** no cookies, no device identifiers, no fingerprinting, no persistent IDs - and requests carrying `DNT: 1` / `Sec-GPC: 1` are dropped before anything is recorded. IP hashes rotate every 24h and raw IPs are never stored; retention is bounded by default (180 days). The full notice is served at `/privacy`.

**Do you need a cookie banner?** Usually no - the collector is fully server-side: nothing is stored on or read from the visitor's device (no cookies, ETags, or localStorage), and ePrivacy Art. 5(3)/PECR/TDDDG §25 consent rules attach to *device access*, not to server-side measurement. Processing still needs a lawful basis under GDPR (commonly legitimate interests, Art. 6(1)(f)), which is an operator decision and duty, not a software property. See [doc/COMPLIANCE.md](doc/COMPLIANCE.md) for the full audit - including the DE/FR caveats and the exact changes that would re-open the banner question.

- **GDPR/ePrivacy (EU):** pseudonymised IP hashes are still personal data (EDPB Guidelines 01/2025), so document a legitimate-interest assessment / DPIA, publish the `/privacy` page, and name a contact.
- **UK GDPR / PECR:** mirrors the EU analysis; the "terminal equipment" test reads the same way. Operator decision.
- **US:** no banner typically required for cookie-free, non-identifying analytics; GPC is honored, which supports state-law universal opt-out positions (CCPA/CPRA, VCDPA, CPA...). Operator decision.

Compliance depends on how *you* deploy and document it - this software provides the technical measures, not a legal guarantee. A per-jurisdiction audit with an operator checklist lives in [doc/COMPLIANCE.md](doc/COMPLIANCE.md).

---

## Erasure & retention operations

- **Automatic:** events older than `RETENTION_DAYS` (default 180) are deleted daily.
- **Per-doc erasure:** call `await db.purge_doc("doc-key")` to hard-delete every event for one doc (GDPR Art. 17 helper).
- **Individual erasure limits:** with a rotating salt and no identifiers, the collector generally cannot link stored events back to a person - say so plainly in your privacy notice.

---

## Scaling notes

**Run a single worker.** The shipped `statless` entrypoint uses one process, and that's deliberate:

- The IP-hash salt and the rate limiter live in process memory, so multiple workers would multiply uniques and rate limits independently (N workers ≈ N× uniques, N× rate limit).
- SQLite is single-writer - extra workers only contend on the write lock.

One event loop easily serves thousands of pixel/heartbeat requests per second; the DB is the bottleneck, not Python.

**If you outgrow it:** move to PostgreSQL, run N replicas, and delegate rate limiting to your proxy (e.g. nginx `limit_req`). Hashing across workers then requires a deterministic, date-based salt derived from a managed `SERVER_SECRET` - planned but not implemented; the current in-memory salt is single-process by design.

---

## License

Distributed under the [AGPLv3](LICENSE). See [`LICENSE`](LICENSE) for more information.
