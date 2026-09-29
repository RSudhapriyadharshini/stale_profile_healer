# Source Health Ledger — Run (import) vs ReRun (real verification)

Two clearly separate operations, on purpose:

- **Run** reads a CSV and creates/updates records. **No network request is
  made.** Every new row gets `created_status_code=200`, `status="active"`.
  This exists to build a controlled dataset to test ReRun against.
- **ReRun** makes the real request, through the proxy, for **every existing
  tracked record** — new or old, it doesn't matter. It's the only thing that
  ever sets `updated_status_code` / `status` / `verification_error`.

An existing record is never treated as evidence that a profile is still
active. Only ReRun decides that, every time it runs.

```bash
pip install -r requirements.txt
python3 main.py run data/sample_urls.csv     # import, no network
python3 main.py rerun                        # real verification, no CSV needed
```
```bash
python3 console.py     # http://localhost:8765 — Run / ReRun buttons
```

## The two operations

```
Run                                    ReRun
────────────────────────               ────────────────────────
CSV row                                every tracked record
   │                                      │
   ▼                                      ▼
resolve to a record                   fetch through the proxy, real
(reuse / retire-on-edit / new)           │
   │                                      ▼
created_status_code = 200             classify (redirect landing, soft-404
status = "active"                     body text, network failure kind, ...)
(existing records: untouched)            │
                                          ▼
                                       updated_status_code / status /
                                       verification_error — always written,
                                       created_status_code untouched
```

## Status mapping (ReRun only — one-shot, no confirmation delay)

| Result | `updated_status_code` | `status` |
|---|---|---|
| 200, and the page is a real individual profile | the code (200) | `active` |
| Redirected to another specific-looking page on the same host, confirmed valid | the landing page's code (200) | `active` |
| 404 / 410 | the code | `inactive` |
| A 200 page whose text says the profile is gone | the code (200) | `inactive` |
| Redirected to a different host, or to a shallow/listing page on the same host (e.g. Compass's `/agents/`) | the code the URL itself returned (e.g. 302) | `inactive` |
| Any other HTTP result — 403, 302 landing elsewhere blocked, 429, 500, 502, 503, 504, 550, ... | the raw code | `suspect` |
| Timeout / DNS failure / connection reset / TLS error / proxy failure | `null` — never an invented HTTP code | `suspect`, with `verification_error` set (`TIMEOUT`, `DNS_ERROR`, `CONNECTION_RESET`, `TLS_ERROR`, `PROXY_CONNECT_ERROR`, `PROXY_AUTH_ERROR`) |

A single confirmed `404` is `inactive` immediately — there is deliberately no
multi-day confirmation step in this version (an earlier version of this
project required two "gone" results days apart before deactivating; this
one, per instruction, does not).

Each URL's status stands entirely on its own. There is no person-level
rollup — `profile_type` (`primary`/`secondary`) is a label on the record,
not something that lets one record override another's status.

## Verified against your real observations

I reproduced all four with a fake network layer standing in for the proxy
(`main.py`/`console.py` themselves always use the real one):

| URL | What you saw | Result here |
|---|---|---|
| `compass.com/agents/andrew-sohn/` | redirects to `/agents/`, profile gone | `inactive`, `updated_status_code=302` |
| `elliman.com/agent/ace-lahli/...` | Page Not Found | `inactive`, `updated_status_code=404` |
| `raveis.com/Agent/imCurley-Egan/...` | 200 or 403/550 depending on proxy IP | 200 → `active`; 550 → `suspect` (checked both) |
| `realtor.com/realestateagents/...` | 200 | `active` |

`created_status_code` stayed `200` throughout every case above — only
`updated_status_code` and `status` moved.

## Files

```
main.py                       CLI: run <csv> | rerun
console.py                    web console: Run / ReRun buttons
source_health/
  classify.py                   HTTP result -> ErrorClass (unchanged, reused as-is)
  verification.py               NEW — ErrorClass -> (status, updated_status_code, verification_error), one-shot
  proxy.py                      the only way a request leaves the machine (unchanged)
  storage.py                    SQLite — one flat `records` table (rewritten: no more profiles+links split)
  pipeline.py                   import_csv() = Run; rerun() = ReRun
  util.py                       URL matching key, domain allow-list check, name matching (unchanged)
data/sample_urls.csv           your CSV, unchanged — the 4 real URLs above are already in it
ledger.db                     created on first run; delete it to start over
tests/                        test_classify, test_proxy (unchanged), test_verification, test_pipeline (both new)
```

`state.py` (the old confirm-over-days state machine) is gone — this version
doesn't need it. `models.py`/`ledger.py`/`fetch_with_tracking.py`-style
per-link/profile split is gone too, replaced by one flat table.

## Record fields

```
record_id, url, url_key, name, source_name, profile_type,
created_status_code, created_at,           <- set once, by Run
updated_status_code, status,
verification_error, verification_attempted_at,   <- set only by ReRun, every time
tracked, superseded_by                       <- CSV-editing housekeeping (see below)
```

## CSV-editing behavior (unchanged from the previous version, still useful, not asked for or against)

- **Edit a URL for the same person+source in the CSV**: the old record is
  auto-retired (`tracked=0`, `superseded_by` set); a fresh record is created
  (`created_status_code=200`, `status=active`) and keeps the same `name`.
- **Remove a row from the CSV**: that record is flagged `tracked=0`
  ("no longer tracked"), excluded from future ReRuns. Reappears and it's
  simply re-tracked, not duplicated.
- `sources` (allowed hosts per `source_name`) are captured at Run time, so
  **ReRun needs no CSV at all** — it reads only from `ledger.db`.

## Safety carried over unchanged

- A request only ever goes to a host that was in the CSV for that
  `source_name` at Run time. A redirect off those hosts is recorded but
  never followed.
- Retry policy is unchanged: transient errors (timeouts, 5xx, 429 honoring
  `Retry-After`) retry with backoff; `403`/blocks and confirmed `404`s never
  retry; wrong proxy credentials (`401`/`407`, live-tested against your real
  Oxylabs account) never retry either, since that won't fix itself on a
  timer.

## Proxy setup

Unchanged from before — see `.env`. Still Oxylabs' Web Scraper API "Proxy
Endpoint" (`realtime.oxylabs.io:60000`, `https://` scheme, TLS verification
off, per their own docs). **Still open:** your credentials were last
returning `401 Unauthorized` live — check your Oxylabs dashboard for whether
`resi_065b9467` belongs to this product or to Residential Proxies
(`pr.oxylabs.io:7777`, different wiring). Tell me if it's the latter.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
60 tests, no network needed. `test_pipeline.py` includes the exact
regression scenarios from the spec: existing `active` → ReRun `404` →
`inactive`; existing `suspect` → ReRun `200` → `active`; `created_status_code`
never overwritten; `profile_type` never reset by ReRun; a second `Run` never
resets a ReRun result.
