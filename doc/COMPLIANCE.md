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
| `ip_hash` | HMAC-SHA256 of the client IP under a RAM-only salt, rotated every `SALT_ROTATE_HOURS` (default 24h) |
| `country` | 2-letter ISO country code from GeoLite2 |
| `referrer` | An approved `?ref=` tag, or the URL reduced to scheme and host |
| `ua` | User-Agent header, truncated to 512 chars |
| `dwell_seconds` | One of 15/30/60/120 (embed heartbeats only) |

The collector never stores raw IP addresses, never sets cookies or ETags, does
no fingerprinting, and records nothing for requests carrying `DNT: 1` or
`Sec-GPC: 1`. Events older than `RETENTION_DAYS` (default 180) are deleted
daily. `purge_doc()` hard-deletes every event for one doc key.

These properties were verified against the source code: no `Set-Cookie`, ETag,
localStorage, sessionStorage, or indexedDB usage exists anywhere in the
collector.

## Consent-banner question

**In most deployments this collector does not require a cookie banner. The
final judgment depends on jurisdiction and is the operator's decision.**

The consent rules in ePrivacy Directive Art. 5(3), PECR, and TDDDG §25 (DE)
attach to storage on, or access to, the visitor's terminal equipment. This
collector is entirely server-side: nothing is written to or read from the
visitor's device, no third-party JavaScript is loaded, and the embed's
heartbeat script talks only to the operator's own host. The consent rule is
therefore not triggered by the tracker itself.

Jurisdiction-specific considerations:

- **Germany (strictest):** §25 TDDDG provides no legitimate-interest route -
  consent is required whenever a technology stores or reads anything on the
  device, regardless of GDPR balancing. The server-side architecture shipped
  here does not touch the device, which is what keeps the banner unnecessary.
  Adding client-side storage (for example a localStorage dedupe) re-opens the
  question.
- **France (CNIL):** CNIL has historically required consent for analytics
  measurement unless the configuration meets its strict exemption list.
  Consider consent when serving French audiences at scale.
- **UK (UK GDPR / PECR):** the "terminal equipment" test reads the same way as
  in the EU. Server-side-only measurement is the safer reading.
- **US:** no consent banner is typically required for cookie-free,
  non-identifying analytics. The collector honors GPC (`Sec-GPC: 1`), which
  supports state-law universal opt-out positions (CCPA/CPRA, VCDPA, CPA).
  "Sale/share" analysis is operator-specific.

## GDPR obligations checklist

Operator checklist mapping each GDPR principle to what the software provides
and what remains the operator's responsibility:

| Obligation | What the software provides | Operator responsibility |
|---|---|---|
| Lawful basis (Art. 6) | Pseudonymised IP hashes are personal data (EDPB Guidelines 01/2025); commonly run on legitimate interests, Art. 6(1)(f) | Document a legitimate-interest assessment; publish the `/privacy` page; name a contact |
| Transparency (Art. 13/14) | `/privacy` discloses every stored field, retention, and opt-out | Keep the notice in sync with configuration (e.g. retention changes) |
| Data minimisation (Art. 5(1)(c)) | No raw IPs, origin-only referrers, 4-value dwell buckets, coarse device labels | Do not add identifiers |
| Storage limitation (Art. 5(1)(e)) | Daily pruning at `RETENTION_DAYS` (default 180); `0` disables pruning | Set a defensible value; keep `0` off unless deletion is operated manually |
| Integrity and confidentiality (Art. 32) | Salt exists only in process memory | Serve behind TLS; restrict host access to the data directory |
| Erasure (Art. 17) | Rotating salt plus no identifiers generally prevents re-identification; `purge_doc()` deletes all events for one doc key | State plainly in the notice that individual erasure is generally not possible |
| Objection and opt-out (Art. 21) | `DNT: 1` and `Sec-GPC: 1` requests are dropped before anything is recorded | Document GPC handling in the notice |
| Records of processing (Art. 30) | Not provided | Add this collector to your record of processing activities |
| Controller role | The operator is the sole controller for stored data | Name the controller in the privacy notice |

## Implementation notes for the DPIA

Two implementation details are worth stating explicitly so that a reviewer
does not mistake them for oversights:

- **Open CORS policy.** `allow_origins=["*"]` exists because heartbeat beacons
  originate from arbitrary framing origins (any allowed embed origin), and the
  collector involves no credentials or cookies. Consequence: the API reads
  data cross-origin too, but `/stats` is public-by-design (use `STATS_TOKEN`
  to gate it).
- **Raw User-Agent stored server-side.** The full UA (truncated to 512 chars)
  is stored in the events table, while `/stats` exposes only coarse family
  labels. The `/privacy` notice discloses this. Storing only the classified
  label would be the stricter minimisation alternative.

## Configurations that change the assessment

- Adding cookies, ETags, or client-side storage/fingerprinting - the
  consent-banner question re-opens.
- Storing raw IPs or full referrer paths - minimisation is lost; a DPIA
  becomes necessary.
- Setting `RETENTION_DAYS=0` - automatic deletion is disabled and the storage
  limitation duty passes to the operator.
- Moving the database (Postgres) to another region - third-country transfer
  rules may then apply; by default all data stays on the operator's host.
