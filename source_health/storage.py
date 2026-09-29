"""
SQLite persistence — one flat table per URL record, real history across runs.

  records   one row per URL. created_status_code / created_at are set once by
            Run (import) and never touched again. updated_status_code /
            status / verification_error / verification_attempted_at are set
            only by ReRun, every time. `tracked=0` means the row is no longer
            in the CSV; `superseded_by` points at the record that replaced it
            when a URL was edited in the sheet.
  sources   each source's allowed hosts, captured at Run time so ReRun needs
            no CSV — it verifies whatever is already in `records`.
  attempts  append-only: one row per real request ReRun made, success or not.
  events    what changed and when, for the "changes this run" report.
  runs      one row per Run/ReRun pass, with its coverage summary.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

SCHEMA = """
create table if not exists records (
    record_id                  text primary key,
    url                        text not null,
    url_key                    text not null,          -- normalize_url(url); unique among tracked records
    name                       text not null,
    source_name                text not null,
    profile_type               text not null,          -- primary | secondary
    created_status_code        integer,                -- set once, by Run: always 200
    created_at                 text,
    updated_status_code        integer,                -- set only by ReRun; null until the first ReRun
    status                     text not null default 'active',  -- active | inactive | suspect
    verification_error         text,                   -- TIMEOUT | DNS_ERROR | ... ; null unless a network-level failure
    verification_attempted_at  text,                    -- last ReRun attempt, whatever the result
    comment                    text,                   -- plain-English reason for the current status (for tracking/debugging)
    tracked                    integer not null default 1,
    superseded_by              text references records(record_id)
);
create index if not exists records_source_idx on records(source_name);

create table if not exists sources (
    source_name   text primary key,
    allowed_hosts text not null default '[]'
);

create table if not exists attempts (
    attempt_id          text primary key,
    run_id              text not null,
    record_id           text,
    url                 text not null,
    http_status         integer,
    final_status        integer,
    final_url           text,
    error_class         text not null,
    reason              text not null,
    verification_error  text,
    retry_no            integer not null default 0,
    attempted_at        text not null
);
create index if not exists attempts_run_idx on attempts(run_id);

create table if not exists events (
    event_id   text primary key,
    run_id     text,
    type       text not null,
    record_id  text not null,
    at         text not null,
    payload    text not null default '{}'
);
create index if not exists events_run_idx on events(run_id);

create table if not exists runs (
    run_id        text primary key,
    kind          text not null,          -- import (Run) | rerun (ReRun)
    source_name   text not null,
    started_at    text not null,
    finished_at   text,
    expected      integer,
    attempted     integer,
    not_attempted integer,
    coverage_pct  real
);
"""


@dataclass
class Record:
    record_id: str
    url: str
    url_key: str
    name: str
    source_name: str
    profile_type: str
    created_status_code: Optional[int] = None
    created_at: Optional[str] = None
    updated_status_code: Optional[int] = None
    status: str = "active"
    verification_error: Optional[str] = None
    verification_attempted_at: Optional[str] = None
    comment: Optional[str] = None
    tracked: bool = True
    superseded_by: Optional[str] = None


@dataclass
class Attempt:
    attempt_id: str
    run_id: str
    record_id: Optional[str]
    url: str
    http_status: Optional[int]
    final_status: Optional[int]
    final_url: Optional[str]
    error_class: str
    reason: str
    verification_error: Optional[str]
    retry_no: int
    attempted_at: str


@dataclass
class Event:
    event_id: str
    type: str
    record_id: str
    at: str
    payload: dict = field(default_factory=dict)
    run_id: Optional[str] = None


class Storage:
    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        try:  # migrate a ledger.db created before the `comment` column existed
            self.conn.execute("alter table records add column comment text")
        except sqlite3.OperationalError:
            pass  # already has it
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- records

    def put_record(self, r: Record) -> None:
        self.conn.execute(
            "insert into records values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) on conflict(record_id) do update set "
            "url=excluded.url, url_key=excluded.url_key, name=excluded.name, source_name=excluded.source_name, "
            "profile_type=excluded.profile_type, created_status_code=excluded.created_status_code, "
            "created_at=excluded.created_at, updated_status_code=excluded.updated_status_code, "
            "status=excluded.status, verification_error=excluded.verification_error, "
            "verification_attempted_at=excluded.verification_attempted_at, comment=excluded.comment, "
            "tracked=excluded.tracked, superseded_by=excluded.superseded_by",
            (
                r.record_id, r.url, r.url_key, r.name, r.source_name, r.profile_type, r.created_status_code,
                r.created_at, r.updated_status_code, r.status, r.verification_error, r.verification_attempted_at,
                r.comment, int(r.tracked), r.superseded_by,
            ),
        )
        self.conn.commit()

    def get_record(self, record_id: str) -> Optional[Record]:
        row = self.conn.execute("select * from records where record_id=?", (record_id,)).fetchone()
        return _record_from_row(row) if row else None

    def get_record_by_url_key(self, url_key: str) -> Optional[Record]:
        row = self.conn.execute("select * from records where url_key=?", (url_key,)).fetchone()
        return _record_from_row(row) if row else None

    def find_tracked_record(self, source_name: str, name: str) -> Optional[Record]:
        """The currently-tracked record for this person on this source, whatever its URL — detects a CSV URL edit."""
        from .util import normalize_person_name

        key = normalize_person_name(name)
        for row in self.conn.execute("select * from records where source_name=? and tracked=1", (source_name,)):
            if normalize_person_name(row["name"]) == key:
                return _record_from_row(row)
        return None

    def list_records(self, source_name: Optional[str] = None, tracked_only: bool = False) -> list[Record]:
        q, args = "select * from records where 1=1", []
        if source_name is not None:
            q += " and source_name=?"
            args.append(source_name)
        if tracked_only:
            q += " and tracked=1"
        q += " order by name"
        return [_record_from_row(r) for r in self.conn.execute(q, args)]

    # ---- sources (allowed hosts persisted at Run time, read back by ReRun)

    def put_source_hosts(self, source_name: str, hosts: list[str]) -> None:
        existing = set(self.get_source_hosts(source_name))
        merged = sorted(existing | set(hosts))
        self.conn.execute(
            "insert into sources values (?,?) on conflict(source_name) do update set allowed_hosts=excluded.allowed_hosts",
            (source_name, json.dumps(merged)),
        )
        self.conn.commit()

    def get_source_hosts(self, source_name: str) -> list[str]:
        row = self.conn.execute("select allowed_hosts from sources where source_name=?", (source_name,)).fetchone()
        return json.loads(row["allowed_hosts"]) if row else []

    def list_source_names(self) -> list[str]:
        return [r["source_name"] for r in self.conn.execute("select source_name from sources order by source_name")]

    # ---- attempts

    def put_attempt(self, a: Attempt) -> None:
        self.conn.execute(
            "insert into attempts values (?,?,?,?,?,?,?,?,?,?,?,?)",
            (a.attempt_id, a.run_id, a.record_id, a.url, a.http_status, a.final_status, a.final_url,
             a.error_class, a.reason, a.verification_error, a.retry_no, a.attempted_at),
        )
        self.conn.commit()

    def list_attempts(self, run_id: Optional[str] = None, record_id: Optional[str] = None) -> list[Attempt]:
        q, args = "select * from attempts where 1=1", []
        if run_id is not None:
            q += " and run_id=?"
            args.append(run_id)
        if record_id is not None:
            q += " and record_id=?"
            args.append(record_id)
        q += " order by attempted_at, attempt_id"
        return [Attempt(**dict(r)) for r in self.conn.execute(q, args)]

    # ---- events

    def add_event(self, e: Event) -> None:
        self.conn.execute(
            "insert into events values (?,?,?,?,?,?)",
            (e.event_id, e.run_id, e.type, e.record_id, e.at, json.dumps(e.payload)),
        )
        self.conn.commit()

    def list_events(self, run_id: Optional[str] = None) -> list[Event]:
        q, args = "select * from events where 1=1", []
        if run_id is not None:
            q += " and run_id=?"
            args.append(run_id)
        q += " order by at, event_id"
        return [_event_from_row(r) for r in self.conn.execute(q, args)]

    # ---- runs

    def put_run(self, run_id: str, kind: str, source_name: str, started_at: str) -> None:
        self.conn.execute(
            "insert into runs (run_id, kind, source_name, started_at) values (?,?,?,?)",
            (run_id, kind, source_name, started_at),
        )
        self.conn.commit()

    def finish_run(self, run_id: str, finished_at: str, expected: int, attempted: int, not_attempted: int, coverage_pct: float) -> None:
        self.conn.execute(
            "update runs set finished_at=?, expected=?, attempted=?, not_attempted=?, coverage_pct=? where run_id=?",
            (finished_at, expected, attempted, not_attempted, coverage_pct, run_id),
        )
        self.conn.commit()

    def list_runs(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("select * from runs order by started_at desc"))


def _record_from_row(row: sqlite3.Row) -> Record:
    d = dict(row)
    d["tracked"] = bool(d["tracked"])
    return Record(**d)


def _event_from_row(row: sqlite3.Row) -> Event:
    d = dict(row)
    d["payload"] = json.loads(d.pop("payload"))
    return Event(**d)
