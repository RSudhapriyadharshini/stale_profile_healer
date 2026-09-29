# Source Health Ledger

Tracks profile URLs and checks whether each one is still active.

- **Run** — reads a CSV and adds/updates records. No network request. Every new row starts as `status = active`.
- **ReRun** — makes the real HTTP request (through a proxy) for every tracked record and updates its actual status: `active`, `inactive`, or `suspect`.

## Setup

```bash
pip install -r requirements.txt
```

Edit `.env` with your proxy details:
```
PROXY_ENDPOINT=realtime.oxylabs.io:60000
PROXY_USERNAME=...
PROXY_PASSWORD=...
```

## Run it

**Command line:**
```bash
python3 main.py run data/sample_urls.csv   # import the CSV, no network
python3 main.py rerun                      # verify every record for real
```

**Or a web page with buttons:**
```bash
python3 console.py
```
Open http://localhost:8765 — Run / ReRun buttons, and a table of every record.

**Testing without a proxy:**
```bash
python3 main.py rerun --no-proxy
```

## What each record looks like

| Field | Meaning |
|---|---|
| `created_status_code` | Always `200`. Set once, by Run. Never changes. |
| `updated_status_code` | The real HTTP code from the last ReRun (or blank if it was a network error). |
| `status` | `active`, `inactive`, or `suspect` — decided fresh by every ReRun. |
| `verification_error` | Set only for network failures (timeout, DNS, proxy issues) — never a made-up HTTP code. |
| `comment` | Plain-English reason for the status, e.g. "HTTP 404 — confirmed not found." |

A 200 page is `active`. A 404, or a redirect to a generic listing page (not the person's own page), is `inactive`. Anything else — 403, 5xx, timeouts, unrecognized codes — is `suspect`: not confirmed either way.

## Files

```
main.py                  command line
console.py               web page with Run/ReRun buttons
source_health/
  classify.py               turns one HTTP result into ok / gone / blocked / etc.
  verification.py           turns that into active / inactive / suspect + comment
  proxy.py                  makes the real request through the proxy
  pipeline.py               Run and ReRun, CSV loading
  storage.py                SQLite database (ledger.db)
data/sample_urls.csv     example CSV
tests/                   python3 -m unittest discover -s tests
```

## CSV format

```csv
name,source_name,role,url
Jane Doe,compass,primary,https://www.compass.com/agents/jane-doe/
```
`role` is `primary` or `secondary` — just a label kept alongside the result, doesn't affect verification.
