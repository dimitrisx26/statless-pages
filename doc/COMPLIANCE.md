# statless-pages compliance audit

> **Not legal advice.** This is an engineering self-assessment of what the
> software does and which legal questions it leaves to the operator. Run your
> own assessment (a DPIA or legitimate-interest record for EU deployments)
> before going live.

## What the collector does

Per tracked fetch (pixel, embed, or badge) the collector stores one database
row:

| Field | Content |
|---|---|
| `doc_key` | The page slug chosen by the operator |
| `ts` | UTC timestamp of the fetch |
| `ip_hash` | HMAC-SHA256 of the client IP under a salt rotated every `SALT_ROTATE_HOURS` (default 24h) |
| `country` | 2-letter ISO country code from GeoLite2 (or `XX`) |
| `referrer` | An approved `?ref=` tag, or the URL reduced to scheme and host |
| `device` | Coarse browser-family label (e.g. `desktop-chrome`, `mobile-safari`, `other`) |
| `dwell_seconds` | One of 15/30/60/120 (embed heartbeats only, NULL for views) |

The collector never stores raw IP addresses, never stores raw User-Agent strings,
never sets tracking cookies or ETags, does no hardware fingerprinting, and records
nothing for requests carrying `DNT: 1`, `Sec-GPC: 1`, `X-Opt-Out: 1`, or `statless_opt_out` cookies.
Events older than `RETENTION_DAYS` (default 180) are deleted daily. `purge_doc()`
hard-deletes all events for one doc key.

These properties were verified against the source code: no tracking cookies,
localStorage, sessionStorage, or indexedDB usage exists anywhere in the collector.
An opt-out functional cookie is set only upon explicit user request at `/opt-out`.

## Consent-banner & ePrivacy analysis

**The necessity of a consent banner depends on the integration method and national jurisdiction. It is the operator's decision.**

The consent rules in ePrivacy Directive Art. 5(3), PECR, and TDDDG §25 (DE)
attach to storage on, or gaining access to information stored in, the visitor's
terminal equipment:

- **SVG Pixel (`<img src="/pixel/key.svg">`):** Operates as a standard HTTP GET
  request. No client-side script is executed, and no storage on the device is used.
  Under the traditional reading of Art. 5(3), standard server-side image fetches
  do not touch terminal equipment. Note, however, that EDPB Guidelines 2/2023
  take an expansive view that tracking pixels triggering data emissions can fall
  under Art. 5(3) across certain EU jurisdictions.
- **Embed Widget (`<iframe src="/embed/key">`):** Injects client-side JavaScript
  (`embed.html`) that executes active timers (`setTimeout`), listens to
  `document.visibilitychange`, and dispatches dwell telemetry via `navigator.sendBeacon`.
  This executes code on the user's terminal equipment.

Jurisdiction-specific considerations:

- **Germany (strictest - §25 TDDDG):** §25(1) TDDDG requires prior consent for
  any access to or storage on terminal equipment unless strictly necessary under
  §25(2) to provide a telemedia service explicitly requested by the user.
  Audience measurement is not deemed "strictly necessary" by the German DSK.
  Using the `/embed` widget in Germany without a consent banner carries regulatory
  risk; using the no-JS SVG pixel is the significantly lower-risk approach.
- **France (CNIL):** CNIL permits an exemption from consent for audience
  measurement trackers provided strict criteria are met (first-party only, anonymous
  statistics, limited retention, no cross-site profiling). Crucially, CNIL
  requires an easily accessible **direct opt-out mechanism** on the site.
  Statless-pages satisfies this via `/opt-out` (supported by functional opt-out cookies,
  `Sec-GPC: 1`, and `DNT: 1`).
- **UK (UK GDPR / PECR):** The ICO draws a distinction between device storage
  and server-side measurement. Server-side pixel measurement without device access
  is generally defensible under legitimate interests without a PECR consent banner.
- **US (CCPA/CPRA, VCDPA, CPA):** No consent banner is typically required for
  first-party, non-profiling analytics. Statless-pages honors Global Privacy Control
  (`Sec-GPC: 1`), satisfying universal opt-out signal mandates under California and
  state privacy laws. There is no "sale" or "sharing" of personal data.

## GDPR obligations checklist

| Obligation | What the software provides | Operator responsibility |
|---|---|---|
| **Lawful basis (Art. 6)** | Pseudonymised IP hashes are personal data (EDPB Guidelines 01/2025); commonly run on legitimate interests (Art. 6(1)(f)) | Document a legitimate-interest assessment (LIA); publish the `/privacy` notice; name a controller and contact |
| **Transparency (Art. 13/14)** | `/privacy` discloses stored fields, retention, legal basis, data subject rights, complaint rights, and opt-out routes | Customize `CONTROLLER_NAME` and `CONTROLLER_CONTACT` in configuration |
| **Data minimisation (Art. 5(1)(c))** | No raw IPs stored; raw User-Agents classified at ingest and discarded immediately; origin-only referrers; 4-value dwell buckets | Do not inject custom identifiers into page keys or `ref` tags |
| **Storage limitation (Art. 5(1)(e))** | Daily pruning at `RETENTION_DAYS` (default 180); `0` disables pruning | Set a defensible retention period; maintain automatic pruning |
| **Data Protection by Default (Art. 25(2))** | `STATS_TOKEN` gates `/stats`, `/overview`, and `/export` against unauthorized public discovery | Set `STATS_TOKEN` before going public so doc keys and stats are not publicly accessible |
| **Integrity and confidentiality (Art. 32)** | Salt rotation; rate-limiting against flood abuse | Serve behind TLS; protect `./data` storage directory; keep secrets secure |
| **Erasure (Art. 17) & Article 11(2)** | Ephemeral salts and no identifiers prevent individual re-identification. `DELETE /docs/{doc_key}` provides bulk doc lifecycle purge | State plainly in notice that individual erasure is governed by Art. 11(2) (non-identifiable data) |
| **Access & Portability (Art. 15/20)** | `GET /export/{doc_key}` streams raw event rows for one doc as an operator backup / audit tool | Do NOT provide `/export` dumps to individual data subjects, as this would disclose other visitors' records (Art. 15(4) / Art. 33) |
| **Objection & Opt-out (Art. 21)** | Honors `Sec-GPC: 1`, `DNT: 1`, `X-Opt-Out: 1`, `?optout=1`, and dedicated `/opt-out` toggle page | Maintain links to `/opt-out` and document GPC support |
| **Records of processing (Art. 30)** | Technical architecture documentation | Add this analytics deployment to your internal Record of Processing Activities |

## Implementation notes for the DPIA

- **Cryptographic Model & `SERVER_SECRET`:**
  - *Ephemeral Salt (Default):* The salt is generated randomly in RAM (`secrets.token_hex(32)`) and never written to disk. When the salt rotates every 24h, previous hashes become cryptographically irrecoverable (true forward-secrecy).
  - *Deterministic Salt (`SERVER_SECRET` configured):* Salts derive from `HMAC(secret, date + window)`. This enables consistency across restarts and multiple container replicas. **DPIA Caveat:** Because IPv4 has only $\approx 3.7$ billion routable addresses, anyone with access to `SERVER_SECRET` and the stored database can reverse-engineer IPv4 addresses via GPU brute force in minutes. Protect `SERVER_SECRET` with the same rigor as raw personal data.
- **Data Minimisation of User-Agent:**
  - Raw User-Agent headers (which can reach 512+ bytes and contain unique device/build fingerprints) are **never written to disk**. They are mapped at the ingestion edge to coarse family labels (e.g. `desktop-chrome`, `mobile-safari`, `other`) via `classify_device()`, and the raw header is immediately discarded.
- **Public vs. Protected Exports:**
  - When `STATS_TOKEN` is unset (public mode), `GET /export/{doc_key}` strips both `ip_hash` and `device` to prevent public correlation of visitor trails. When `STATS_TOKEN` is authenticated, full audit rows are exported for operator backup purposes.
- **Open CORS Policy:**
  - `allow_origins=["*"]` allows embed beacons to report from any framing origin (e.g. Notion, customer websites). Because no cookies or credentials are used in analytics ingestion, cross-origin request forgery (CSRF) is not a risk for collection. Setting `STATS_TOKEN` protects sensitive reporting endpoints from unauthorized cross-origin reads.

## Configurations that change the assessment

- Adding custom client-side tracking, storage, or fingerprinting re-opens the consent-banner requirement in all jurisdictions.
- Putting identifying personal data into `doc_key` or `?ref=` parameters bypasses data minimisation.
- Setting `RETENTION_DAYS=0` disables automatic deletion, passing storage-limitation duties entirely to manual operator processes.
- Deploying database replicas outside the EU/EEA engages GDPR Chapter V international data transfer requirements.
