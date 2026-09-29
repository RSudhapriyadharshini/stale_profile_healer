"""
Run  (import_csv)  ->  CSV rows become records. No network. created_status_code=200,
                        status="active" for every new row. Existing records are left
                        exactly as they are — Run never touches verification fields.

ReRun (rerun)       ->  every tracked record gets a real fetch through the proxy.
                        Only updated_status_code / status / verification_error /
                        verification_attempted_at change. created_status_code and
                        profile_type are never touched.
"""

from __future__ import annotations

import csv as csv_module
import re
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

from .proxy import DisallowedUrl, FetchImpl, fetch_with_retries, make_fetch_impl, require_proxy_config
from .storage import Attempt, Event, Record, Storage
from .util import normalize_person_name, normalize_url
from .verification import verify

REQUIRED_COLUMNS = ("name", "source_name", "role", "url")


class CsvFormatError(Exception):
    pass


@dataclass
class CsvRow:
    line: int
    name: str
    source_name: str
    role: str
    url: str


def load_csv(path: str) -> list[CsvRow]:
    p = Path(path)
    if not p.exists():
        raise CsvFormatError(f"CSV file not found: {p}")
    with p.open(newline="", encoding="utf-8-sig") as f:
        reader = csv_module.DictReader(f)
        headers = {(h or "").strip().lower() for h in (reader.fieldnames or [])}
        missing = [c for c in REQUIRED_COLUMNS if c not in headers]
        if missing:
            raise CsvFormatError(f"{p}: missing column(s) {missing}. Required: {list(REQUIRED_COLUMNS)}")

        rows: list[CsvRow] = []
        seen_url: set[tuple[str, str]] = set()
        seen_person: dict[tuple[str, str], int] = {}
        for i, raw in enumerate(reader, start=2):
            get = lambda k: (raw.get(k) or "").strip()  # noqa: E731
            name, source_name, role, url = get("name"), get("source_name"), get("role").lower(), get("url")
            if not any((name, source_name, role, url)):
                continue
            if not name:
                raise CsvFormatError(f"{p}:{i}: name is required")
            if not source_name:
                raise CsvFormatError(f"{p}:{i}: source_name is required")
            if role not in ("primary", "secondary"):
                raise CsvFormatError(f"{p}:{i}: role must be 'primary' or 'secondary', got {role!r}")
            if not url or not re.match(r"^https?://", url, re.IGNORECASE):
                raise CsvFormatError(f"{p}:{i}: url must start with http:// or https://, got {url!r}")
            if (source_name, url) in seen_url:
                raise CsvFormatError(f"{p}:{i}: duplicate url for source {source_name!r}: {url}")
            seen_url.add((source_name, url))
            person_key = (normalize_person_name(name), source_name)
            if person_key in seen_person:
                raise CsvFormatError(
                    f"{p}:{i}: {name!r} already has a different URL for source {source_name!r} at line "
                    f"{seen_person[person_key]} in this same file — fix the sheet so each person has one row per source."
                )
            seen_person[person_key] = i
            rows.append(CsvRow(line=i, name=name, source_name=source_name, role=role, url=url))
    if not rows:
        raise CsvFormatError(f"{p}: no data rows")
    return rows


def _host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def hosts_by_source(rows: list[CsvRow]) -> dict[str, list[str]]:
    out: dict[str, set[str]] = {}
    for r in rows:
        out.setdefault(r.source_name, set()).add(_host_of(r.url))
    return {k: sorted(v) for k, v in out.items()}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def utc_now_iso() -> str:
    from datetime import datetime, timezone

    now = datetime.now(tz=timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


@dataclass
class SourceRunSummary:
    source_name: str
    kind: str
    expected: int
    attempted: int
    not_attempted: int
    coverage_pct: float
    by_status: dict[str, int]


@dataclass
class RunReport:
    run_ids: list[str]
    summaries: list[SourceRunSummary]
    events: list[Event]
    warnings: list[str]


# ------------------------------------------------------------------ Run (import, no network)


def _resolve_import(db: Storage, row: CsvRow, now: str, events: list[Event], run_id: str) -> Record:
    """
    Map one CSV row to a record. Run NEVER touches verification fields of an
    existing record — only a brand-new URL gets created_status_code=200 /
    status="active" written.
      - same URL already known (tracked or not)           -> just re-track it, untouched otherwise
      - same person+source, URL changed in the CSV         -> retire the old record, create a fresh one
      - never seen before                                   -> new record, created_status_code=200, status=active
    """
    key = normalize_url(row.url)
    existing = db.get_record_by_url_key(key)
    if existing:
        if not existing.tracked:
            existing = replace(existing, tracked=True, superseded_by=None)
            db.put_record(existing)
        return existing

    prior = db.find_tracked_record(row.source_name, row.name)
    if prior:
        new_record = Record(
            record_id=new_id("rec"), url=row.url, url_key=key, name=row.name, source_name=row.source_name,
            profile_type=row.role, created_status_code=200, created_at=now, status="active",
            comment="Imported from CSV — not yet verified by ReRun.",
        )
        db.put_record(new_record)
        db.put_record(replace(prior, tracked=False, superseded_by=new_record.record_id))
        events.append(Event(new_id("evt"), "record.url_edited_in_csv", new_record.record_id, now, {"from": prior.url, "to": row.url}, run_id))
        return new_record

    new_record = Record(
        record_id=new_id("rec"), url=row.url, url_key=key, name=row.name, source_name=row.source_name,
        profile_type=row.role, created_status_code=200, created_at=now, status="active",
        comment="Imported from CSV — not yet verified by ReRun.",
    )
    db.put_record(new_record)
    events.append(Event(new_id("evt"), "record.added", new_record.record_id, now, {"name": row.name, "profile_type": row.role, "url": row.url}, run_id))
    return new_record


def _untrack_missing(db: Storage, source_name: str, rows: list[CsvRow], now: str, events: list[Event], run_id: str) -> None:
    names_present = {normalize_person_name(r.name) for r in rows}
    for rec in db.list_records(source_name=source_name, tracked_only=True):
        if normalize_person_name(rec.name) not in names_present:
            db.put_record(replace(rec, tracked=False))
            events.append(Event(new_id("evt"), "record.untracked", rec.record_id, now, {"url": rec.url, "reason": "removed_from_csv"}, run_id))


def import_csv(db: Storage, csv_path: str, now_fn: Callable[[], str] = utc_now_iso) -> RunReport:
    """
    Run: read the CSV and create/track records with created_status_code=200,
    status="active". No network request is made. Existing records are left
    exactly as ReRun last set them.
    """
    rows = load_csv(csv_path)
    hosts = hosts_by_source(rows)
    for source_name, source_hosts in hosts.items():
        db.put_source_hosts(source_name, source_hosts)

    rows_by_source: dict[str, list[CsvRow]] = {}
    for r in rows:
        rows_by_source.setdefault(r.source_name, []).append(r)

    run_ids, summaries, all_events = [], [], []
    for source_name, source_rows in rows_by_source.items():
        run_id = new_id("run")
        started_at = now_fn()
        db.put_run(run_id, "import", source_name, started_at)
        run_ids.append(run_id)
        run_events: list[Event] = []

        for row in source_rows:
            _resolve_import(db, row, started_at, run_events, run_id)
        _untrack_missing(db, source_name, source_rows, started_at, run_events, run_id)

        n = len(source_rows)
        db.finish_run(run_id, now_fn(), n, n, 0, 100.0)
        by_status: dict[str, int] = {}
        for r in db.list_records(source_name=source_name, tracked_only=True):
            by_status[r.status] = by_status.get(r.status, 0) + 1
        summaries.append(SourceRunSummary(source_name, "import", n, n, 0, 100.0, by_status))
        for e in run_events:
            db.add_event(e)
        all_events.extend(run_events)

    return RunReport(run_ids, summaries, all_events, warnings=[])


# ------------------------------------------------------------------ ReRun (real verification)


def rerun(
    db: Storage,
    fetch_impl: Optional[FetchImpl] = None,
    sleep: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], str] = utc_now_iso,
    progress: Optional[Callable[[str], None]] = None,
) -> RunReport:
    """
    ReRun: every tracked record gets a real fetch through the proxy. Reads
    only from the database (source hosts persisted by the last Run) — no CSV
    needed. created_status_code and profile_type are never touched here;
    only updated_status_code / status / verification_error /
    verification_attempted_at change (plus the URL itself, if the profile
    moved to another specific page on the same host).
    """
    log = progress or (lambda msg: None)
    if fetch_impl is None:
        cfg = require_proxy_config()
        fetch_impl = make_fetch_impl(cfg)

    run_ids, summaries, all_events, warnings = [], [], [], []
    for source_name in db.list_source_names():
        records = db.list_records(source_name=source_name, tracked_only=True)
        if not records:
            continue
        allowed_hosts = db.get_source_hosts(source_name)
        run_id = new_id("run")
        db.put_run(run_id, "rerun", source_name, now_fn())
        run_ids.append(run_id)
        run_events: list[Event] = []

        log(f"[{source_name}] rerun: {len(records)} record(s) to verify")
        attempted = 0
        for i, rec in enumerate(records, start=1):
            log(f"  ({i}/{len(records)}) verifying {rec.url} ...")

            def _on_attempt(retry_no, c, after_sec, _rec=rec):
                tail = f", retrying in {after_sec:.0f}s..." if after_sec else ""
                log(f"      attempt {retry_no + 1}: {c.error_class} ({c.reason}){tail}")

            try:
                result = fetch_with_retries(rec.url, fetch_impl, allowed_hosts, sleep=sleep, on_attempt=_on_attempt)
            except DisallowedUrl as e:
                warnings.append(f"refused: {rec.url} — {e}")
                log(f"      refused: {e}")
                continue
            attempted += 1
            at = now_fn()

            db.put_attempt(Attempt(
                new_id("att"), run_id, rec.record_id, rec.url, result.observation.http_status,
                result.observation.final_status, result.observation.final_url, result.classification.error_class,
                result.classification.reason, None, result.attempts - 1, at,
            ))

            v = verify(result.classification, result.observation)
            old_status = rec.status
            updated = replace(
                rec,
                updated_status_code=v.updated_status_code,
                status=v.status,
                verification_error=v.verification_error,
                verification_attempted_at=at,
                comment=v.comment,
            )
            if result.classification.error_class == "url_changed" and result.classification.new_url:
                updated.url = result.classification.new_url
                updated.url_key = normalize_url(result.classification.new_url)
            db.put_record(updated)
            rec = updated
            log(f"      -> updated_status_code={v.updated_status_code} status={v.status}"
                + (f" verification_error={v.verification_error}" if v.verification_error else ""))

            if v.status != old_status:
                run_events.append(Event(new_id("evt"), "status_changed", rec.record_id, at, {"from": old_status, "to": v.status}, run_id))

        expected = len(records)
        coverage = 100.0 if expected == 0 else round(100 * attempted / expected, 1)
        db.finish_run(run_id, now_fn(), expected, attempted, expected - attempted, coverage)
        by_status: dict[str, int] = {}
        for r in db.list_records(source_name=source_name, tracked_only=True):
            by_status[r.status] = by_status.get(r.status, 0) + 1
        summaries.append(SourceRunSummary(source_name, "rerun", expected, attempted, expected - attempted, coverage, by_status))
        for e in run_events:
            db.add_event(e)
        all_events.extend(run_events)

    return RunReport(run_ids, summaries, all_events, warnings)
