#!/usr/bin/env python3
"""
Source Health Ledger — CLI.

    python3 main.py run data/sample_urls.csv     # import the CSV. NO network request is made.
    python3 main.py rerun                        # real verification of every tracked record.

Run seeds records with created_status_code=200, status="active" — a
controlled dataset, nothing more. ReRun makes the real proxy fetches and is
the only thing that ever sets updated_status_code / status (active,
inactive, suspect) / verification_error. Both share ledger.db, so ReRun needs
no CSV — it reads whatever Run already put there.
"""

from __future__ import annotations

import argparse
import sys

from source_health.pipeline import CsvFormatError, import_csv, rerun
from source_health.proxy import DisallowedUrl, ProxyNotConfigured, load_env, make_direct_fetch_impl, proxy_config_from_env
from source_health.storage import Storage


def print_report(db: Storage, report) -> None:
    if report.warnings:
        print("\n== Warnings")
        for w in report.warnings:
            print(f"  {w}")

    print("\n== This run")
    for s in report.summaries:
        mix = "  ".join(f"{k}={v}" for k, v in sorted(s.by_status.items()))
        flag = "  <-- some records were not attempted" if s.not_attempted else ""
        print(f"  [{s.source_name}] {s.kind}: expected {s.expected}  attempted {s.attempted}  coverage {s.coverage_pct}%{flag}")
        print(f"      current status mix: {mix or '(no tracked records)'}")

    if report.events:
        print("\n== Changes this run")
        for e in report.events:
            rec = db.get_record(e.record_id)
            name = rec.name if rec else e.record_id
            detail = " ".join(f"{k}={v}" for k, v in e.payload.items() if k not in ("url",))
            print(f"  {e.at[:19].replace('T', ' ')}  {name:<20} {e.type:<24} {detail}")
    else:
        print("\n== Changes this run\n  (none)")

    print("\n== All records")
    for r in db.list_records():
        tag = "" if r.tracked else " [untracked]"
        verr = f" verification_error={r.verification_error}" if r.verification_error else ""
        print(
            f"  {r.name:<20} {r.source_name:<20} {r.profile_type:<10} status={r.status:<9} "
            f"created={r.created_status_code} updated={r.updated_status_code}{verr}{tag}"
        )
        if r.comment:
            print(f"      comment: {r.comment}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["run", "rerun"], help="'run' imports a CSV (no network); 'rerun' verifies for real")
    parser.add_argument("csv", nargs="?", help="CSV file for 'run': name,source_name,role,url")
    parser.add_argument("--db", default="ledger.db", help="SQLite ledger file (default: ledger.db)")
    parser.add_argument("--env", default=".env", help="Path to .env (default: ./.env)")
    parser.add_argument(
        "--proxy", action=argparse.BooleanOptionalAction, default=True,
        help="Route 'rerun' through the configured proxy (default). --no-proxy connects directly instead — "
             "for local testing only (e.g. against httpstat.us or a local server); never the real behavior.",
    )
    args = parser.parse_args()

    load_env(args.env)
    db = Storage(args.db)

    try:
        if args.action == "run":
            if not args.csv:
                print("error: 'run' needs a CSV path, e.g. python3 main.py run data/sample_urls.csv", file=sys.stderr)
                raise SystemExit(1)
            print("Run: importing the CSV. No network request will be made.")
            report = import_csv(db, args.csv)
        else:
            if args.proxy:
                proxy = proxy_config_from_env()
                print(f"Proxy: {proxy.display if proxy else 'NOT CONFIGURED — set PROXY_ENDPOINT in ' + args.env}")
                if proxy is None:
                    print("Refusing to run: nothing is ever sent without a proxy. Pass --no-proxy to connect directly instead.", file=sys.stderr)
                    raise SystemExit(1)
                fetch_impl = None  # rerun() builds it from the configured proxy
            else:
                print("Proxy: DISABLED (--no-proxy) — connecting directly. This does not match production behavior.")
                fetch_impl = make_direct_fetch_impl()
            print("ReRun: verifying every tracked record for real (this can take a while per URL) ...")
            report = rerun(db, fetch_impl=fetch_impl, progress=lambda msg: print(msg, flush=True))
    except (CsvFormatError, ProxyNotConfigured, DisallowedUrl) as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(1)

    print_report(db, report)
    print(f"\nLedger: {args.db}")


if __name__ == "__main__":
    main()
