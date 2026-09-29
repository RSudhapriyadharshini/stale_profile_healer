#!/usr/bin/env python3
"""
Source Health Ledger — web console. One file, standard library only.

    python3 console.py                       # http://localhost:8765
    python3 console.py --csv data/sample_urls.csv --port 9000

Run imports the CSV — NO network request is made; every new row gets
created_status_code=200, status="active". ReRun makes the real proxy fetch
for every tracked record and is the only thing that ever sets
updated_status_code / status / verification_error. Both share ledger.db.
"""

from __future__ import annotations

import argparse
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from source_health.pipeline import CsvFormatError, import_csv, rerun
from source_health.proxy import DisallowedUrl, ProxyNotConfigured, load_env, make_direct_fetch_impl, proxy_config_from_env
from source_health.storage import Storage

DEFAULT_CSV = "data/sample_urls.csv"

CSS = """
:root{--bg:#f6f7f9;--surface:#fff;--border:#dfe3e8;--text:#16191d;--muted:#5d6673;--accent:#2856d8;
--ok:#1d7a46;--ok-soft:#e3f4ea;--warn:#9a5b00;--warn-soft:#fdf0dc;--bad:#b3261e;--bad-soft:#fbe6e4;--neutral:#eceef1}
@media(prefers-color-scheme:dark){:root{--bg:#111317;--surface:#1a1d23;--border:#2f343c;--text:#e8eaed;--muted:#9aa3ae;
--accent:#7ea2ff;--ok:#5cc98a;--ok-soft:#173325;--warn:#f0b35a;--warn-soft:#3a2c14;--bad:#ff8a80;--bad-soft:#3d1c1a;--neutral:#2a2e35}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,sans-serif}
.app{max-width:1150px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:15px;margin:22px 0 8px}
p.lead{color:var(--muted);margin:0 0 16px}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px;margin-bottom:14px}
.controls{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input[type=text]{padding:7px 10px;border:1px solid var(--border);border-radius:8px;background:var(--surface);color:var(--text);min-width:280px}
button{padding:7px 14px;border:1px solid var(--border);border-radius:8px;background:var(--surface);color:var(--text);cursor:pointer;font:inherit}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.rerun{background:var(--ok);border-color:var(--ok);color:#fff}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--border);white-space:nowrap}
th{color:var(--muted);font-weight:600;font-size:12px}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600}
.pill.active{background:var(--ok-soft);color:var(--ok)}
.pill.suspect{background:var(--warn-soft);color:var(--warn)}
.pill.inactive{background:var(--bad-soft);color:var(--bad)}
.pill.untracked,.pill.primary,.pill.secondary{background:var(--neutral);color:var(--muted)}
.mono{font-family:ui-monospace,Menlo,monospace}
.muted{color:var(--muted)}
.alert{background:var(--bad-soft);color:var(--bad);border-radius:8px;padding:8px 10px;margin-bottom:8px}
.empty{color:var(--muted);text-align:center;padding:20px}
"""


def e(v) -> str:
    return escape("" if v is None else str(v))


def pill(value: str) -> str:
    return f'<span class="pill {e(value)}">{e(value)}</span>'


def _latest_per_source(runs, kind: str):
    """runs is newest-first; keep the first (most recent) run of `kind` seen per source_name."""
    seen = set()
    out = []
    for r in runs:
        if r["kind"] == kind and r["source_name"] not in seen:
            seen.add(r["source_name"])
            out.append(r)
    return out


def render(db: Storage, csv_path: str, message: Optional[str] = None, empty: bool = False, use_proxy: bool = True) -> str:
    """
    `empty=True` (a plain page load/refresh, GET /) always shows a blank
    dashboard — the underlying ledger.db is untouched and keeps its history,
    but nothing from it is displayed until you click Run or ReRun again in
    *this* view. Only the POST /run and POST /rerun responses (`empty=False`)
    show real data, so a demo always starts from a clean screen.
    """
    proxy = proxy_config_from_env()
    if not use_proxy:
        proxy_line = f'{pill("suspect")} <span class="muted">proxy DISABLED (--no-proxy) — ReRun connects directly</span>'
    elif proxy:
        proxy_line = f'{pill("active")} <span class="mono muted">{e(proxy.display)}</span>'
    else:
        proxy_line = f'{pill("inactive")} <span class="muted">no proxy configured — set PROXY_ENDPOINT in .env</span>'
    banner = f'<div class="alert">{e(message)}</div>' if message else ""

    if empty:
        runs_table = '<div class="empty">Nothing yet. Press Run.</div>'
        added_table = '<div class="empty">No records added yet.</div>'
        verified_table = '<div class="empty">No status changes yet.</div>'
        records_table = '<div class="empty">No records yet.</div>'
        return _shell(proxy_line, banner, csv_path, runs_table, added_table, verified_table, records_table)

    runs = db.list_runs()
    latest_by_source = {}
    for r in runs:
        latest_by_source.setdefault(r["source_name"], r)  # newest-first already
    run_rows = ""
    for r in latest_by_source.values():
        run_rows += (
            f"<tr><td>{e(r['source_name'])}</td><td>{e(r['kind'])}</td>"
            f"<td>{e((r['started_at'] or '')[:19].replace('T',' '))}</td>"
            f"<td>{e(r['expected'])}</td><td>{e(r['attempted'])}</td>"
            f"<td>{e(r['coverage_pct'])}%</td><td>{e(r['not_attempted'])}</td></tr>"
        )
    runs_table = (
        f'<table><thead><tr><th>Source</th><th>Last action</th><th>When</th><th>Expected</th>'
        f"<th>Attempted</th><th>Coverage</th><th>Not attempted</th></tr></thead><tbody>{run_rows}</tbody></table>"
        if run_rows else '<div class="empty">Nothing yet. Press Run.</div>'
    )

    # Two independent sections, kept separate so clicking ReRun never hides what the last Run added.
    latest_import_ids = {r["run_id"] for r in _latest_per_source(runs, "import")}
    latest_rerun_ids = {r["run_id"] for r in _latest_per_source(runs, "rerun")}

    def events_table_for(run_ids: set, empty_text: str) -> str:
        evs = [ev for ev in db.list_events() if ev.run_id in run_ids] if run_ids else []
        rows = "".join(
            f'<tr><td>{e(ev.at[:19].replace("T"," "))}</td>'
            f'<td>{e((db.get_record(ev.record_id) or type("_",(),{"name":ev.record_id})()).name)}</td>'
            f'<td class="mono">{e(ev.type)}</td>'
            f'<td class="mono muted">{e(" ".join(f"{k}={v}" for k, v in ev.payload.items() if k != "url"))}</td></tr>'
            for ev in evs
        )
        return (
            f'<table><thead><tr><th>When</th><th>Name</th><th>Event</th><th>Detail</th></tr></thead><tbody>{rows}</tbody></table>'
            if rows else f'<div class="empty">{e(empty_text)}</div>'
        )

    added_table = events_table_for(latest_import_ids, "No records added by the last Run.")
    verified_table = events_table_for(latest_rerun_ids, "No status changes from the last ReRun yet.")

    record_rows = ""
    for r in sorted(db.list_records(), key=lambda r: (not r.tracked, r.name, r.source_name)):
        tag = "" if r.tracked else " (untracked)"
        verr = f'<br><span class="muted mono">{e(r.verification_error)}</span>' if r.verification_error else ""
        record_rows += (
            f"<tr><td>{e(r.name)}{tag}</td><td>{e(r.source_name)}</td><td>{pill(r.profile_type)}</td>"
            f"<td>{pill(r.status)}</td><td class=\"mono\">{e(r.created_status_code)}</td>"
            f"<td class=\"mono\">{e(r.updated_status_code) or '–'}{verr}</td>"
            f'<td class="url mono" title="{e(r.url)}">{e(r.url[:60])}</td>'
            f'<td style="white-space:normal;max-width:280px">{e(r.comment) or "–"}</td></tr>'
        )
    records_table = (
        f'<table><thead><tr><th>Name</th><th>Source</th><th>Type</th><th>Status</th>'
        f"<th>created_status_code</th><th>updated_status_code</th><th>URL</th><th>Comments</th></tr></thead><tbody>{record_rows}</tbody></table>"
        if record_rows else '<div class="empty">No records yet.</div>'
    )

    return _shell(proxy_line, banner, csv_path, runs_table, added_table, verified_table, records_table)


def _shell(proxy_line: str, banner: str, csv_path: str, runs_table: str, added_table: str, verified_table: str, records_table: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Source Health Console</title>
<style>{CSS}</style></head><body><div class="app">
<h1>Source Health Console</h1>
<p class="lead"><b>Run</b> imports the CSV — no network request is made; every new row gets
created_status_code=200, status="active". <b>ReRun</b> makes the real request for every
tracked record and is the only thing that changes updated_status_code / status / verification_error.
Refreshing this page always shows a blank dashboard; only the result of clicking Run or ReRun is displayed.</p>
<div class="panel">
  <div class="controls" style="margin-bottom:10px">{proxy_line}</div>
  {banner}
  <form method="post" action="/run" class="controls">
    <input type="text" name="csv" value="{e(csv_path)}">
    <button class="primary">Run (import, no network)</button>
  </form>
  <form method="post" action="/rerun" class="controls" style="margin-top:8px">
    <button class="rerun">ReRun (real verification of every tracked record)</button>
  </form>
  <p class="muted" style="margin:8px 0 0">ReRun blocks until every real fetch finishes — this can take a while
  through a real scraping proxy. The page updates when it's done.</p>
</div>
<h2>Latest action per source</h2>
<div class="panel">{runs_table}</div>
<h2>Newly added / changed by the last Run</h2>
<div class="panel">{added_table}</div>
<h2>Verified by the last ReRun</h2>
<div class="panel">{verified_table}</div>
<h2>All records</h2>
<div class="panel">{records_table}</div>
</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    db: Storage
    default_csv: str = DEFAULT_CSV
    use_proxy: bool = True  # set from --proxy/--no-proxy at startup; --no-proxy is for local testing only

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/":
            self.send_error(404)
            return
        self._respond(render(self.db, self.default_csv, empty=True, use_proxy=self.use_proxy))

    def do_POST(self) -> None:  # noqa: N802
        cls = type(self)
        path = urlparse(self.path).path
        length = int(self.headers.get("content-length") or 0)
        form = parse_qs(self.rfile.read(length).decode())
        message = None
        try:
            if path == "/run":
                csv_path = form.get("csv", [self.default_csv])[0] or self.default_csv
                self.default_csv = csv_path
                report = import_csv(self.db, csv_path)
            elif path == "/rerun":
                fetch_impl = None if cls.use_proxy else make_direct_fetch_impl()
                report = rerun(self.db, fetch_impl=fetch_impl)
            else:
                self.send_error(404)
                return
            if report.warnings:
                message = "Warnings: " + "; ".join(report.warnings)
        except (CsvFormatError, ProxyNotConfigured, DisallowedUrl) as err:
            message = f"error: {err}"
        self._respond(render(self.db, self.default_csv, message, use_proxy=self.use_proxy))

    def _respond(self, html: str) -> None:
        body = html.encode()
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Source Health Console")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--csv", default=DEFAULT_CSV)
    parser.add_argument("--db", default="ledger.db")
    parser.add_argument("--env", default=".env")
    parser.add_argument(
        "--proxy", action=argparse.BooleanOptionalAction, default=True,
        help="Route ReRun through the configured proxy (default). --no-proxy connects directly instead — "
             "for local testing only; never the real behavior.",
    )
    args = parser.parse_args()

    load_env(args.env)
    Handler.db = Storage(args.db)
    Handler.default_csv = args.csv
    Handler.use_proxy = args.proxy
    server = HTTPServer((args.host, args.port), Handler)
    proxy = proxy_config_from_env()
    print(f"Source Health Console on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    if args.proxy:
        print(f"Proxy: {proxy.display if proxy else 'NOT CONFIGURED — set PROXY_ENDPOINT in .env'}")
    else:
        print("Proxy: DISABLED (--no-proxy) — ReRun will connect directly. This does not match production behavior.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
