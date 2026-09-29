"""
Run (import, no network) and ReRun (real verification) through pipeline.py,
with a fake fetch_impl standing in for the proxy for ReRun tests.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from source_health.pipeline import CsvFormatError, import_csv, rerun
from source_health.storage import Storage

CSV_V1 = """name,source_name,role,url
Jane Doe,primary_directory,primary,https://a.test/jane
Jane Doe,secondary_directory,secondary,https://b.test/jane
John Doe,primary_directory,primary,https://a.test/john
"""


def write_csv(text: str) -> str:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8")
    f.write(text)
    f.close()
    return f.name


def fake_fetch(routes: dict):
    """routes: url -> (status, body_str) | Exception"""

    def fetch(url, timeout, headers):
        r = routes.get(url, (404, "unrouted"))
        if isinstance(r, Exception):
            raise r
        status, body = r
        return status, {}, body.encode()

    return fetch


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 9, 27, tzinfo=timezone.utc)

    def advance(self, days: int) -> None:
        self.t += timedelta(days=days)

    def __call__(self) -> str:
        iso = self.t.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.t += timedelta(seconds=1)
        return iso


class RunImportTest(unittest.TestCase):
    """Section 1: Run makes NO network request; every row gets created_status_code=200, status=active."""

    def setUp(self):
        self.db = Storage(":memory:")
        self.clock = Clock()

    def by_name_source(self, name, source_name):
        return next(r for r in self.db.list_records() if r.name == name and r.source_name == source_name)

    def test_every_row_seeded_with_200_active_no_network(self):
        import_csv(self.db, write_csv(CSV_V1), now_fn=self.clock)
        records = self.db.list_records()
        self.assertEqual(len(records), 3)
        for r in records:
            self.assertEqual((r.created_status_code, r.status, r.updated_status_code, r.verification_error), (200, "active", None, None))

    def test_primary_and_secondary_classified_correctly(self):
        import_csv(self.db, write_csv(CSV_V1), now_fn=self.clock)
        self.assertEqual(self.by_name_source("Jane Doe", "primary_directory").profile_type, "primary")
        self.assertEqual(self.by_name_source("Jane Doe", "secondary_directory").profile_type, "secondary")
        self.assertEqual(self.by_name_source("John Doe", "primary_directory").profile_type, "primary")

    def test_rerun_does_not_run_during_import(self):
        """No fetch_impl is even reachable from import_csv — there is no network path to call."""
        import inspect

        self.assertNotIn("fetch_impl", inspect.signature(import_csv).parameters)


class ReRunMappingTest(unittest.TestCase):
    """Section 2/3/4: ReRun performs the real request and maps the result. created_status_code never changes."""

    def setUp(self):
        self.db = Storage(":memory:")
        self.clock = Clock()
        import_csv(self.db, write_csv(CSV_V1), now_fn=self.clock)

    def rec(self, name, source_name):
        return next(r for r in self.db.list_records() if r.name == name and r.source_name == source_name)

    def test_200_keeps_active(self):
        rerun(self.db, fetch_impl=fake_fetch({"https://a.test/jane": (200, "ok"), "https://b.test/jane": (200, "ok"), "https://a.test/john": (200, "ok")}), sleep=lambda s: None, now_fn=self.clock)
        r = self.rec("Jane Doe", "primary_directory")
        self.assertEqual((r.created_status_code, r.updated_status_code, r.status), (200, 200, "active"))

    def test_404_becomes_inactive_immediately(self):
        rerun(self.db, fetch_impl=fake_fetch({"https://a.test/jane": (404, "gone"), "https://b.test/jane": (200, "ok"), "https://a.test/john": (200, "ok")}), sleep=lambda s: None, now_fn=self.clock)
        r = self.rec("Jane Doe", "primary_directory")
        self.assertEqual((r.created_status_code, r.updated_status_code, r.status), (200, 404, "inactive"))

    def test_other_http_errors_become_suspect(self):
        codes = {403: "https://a.test/jane", 503: "https://b.test/jane", 550: "https://a.test/john"}
        routes = {url: (code, "x") for code, url in codes.items()}
        rerun(self.db, fetch_impl=fake_fetch(routes), sleep=lambda s: None, now_fn=self.clock)
        for code, url in codes.items():
            rec = next(r for r in self.db.list_records() if r.url == url)
            self.assertEqual((rec.updated_status_code, rec.status), (code, "suspect"), f"for {code}")

    def test_network_error_is_suspect_with_no_invented_code(self):
        from source_health.proxy import _NetworkError

        def fetch(url, timeout, headers):
            raise _NetworkError("timeout", "slow")

        rerun(self.db, fetch_impl=fetch, sleep=lambda s: None, now_fn=self.clock)
        r = self.rec("Jane Doe", "primary_directory")
        self.assertEqual((r.updated_status_code, r.status, r.verification_error), (None, "suspect", "TIMEOUT"))

    def test_created_status_code_never_overwritten_by_rerun(self):
        rerun(self.db, fetch_impl=fake_fetch({"https://a.test/jane": (404, "gone"), "https://b.test/jane": (200, "ok"), "https://a.test/john": (200, "ok")}), sleep=lambda s: None, now_fn=self.clock)
        rerun(self.db, fetch_impl=fake_fetch({"https://a.test/jane": (200, "ok"), "https://b.test/jane": (200, "ok"), "https://a.test/john": (200, "ok")}), sleep=lambda s: None, now_fn=self.clock)
        r = self.rec("Jane Doe", "primary_directory")
        self.assertEqual(r.created_status_code, 200)  # unchanged across both ReRuns
        self.assertEqual((r.updated_status_code, r.status), (200, "active"))  # latest ReRun result

    def test_profile_type_never_changed_by_rerun(self):
        rerun(self.db, fetch_impl=fake_fetch({"https://a.test/jane": (404, "gone"), "https://b.test/jane": (200, "ok"), "https://a.test/john": (200, "ok")}), sleep=lambda s: None, now_fn=self.clock)
        self.assertEqual(self.rec("Jane Doe", "primary_directory").profile_type, "primary")
        self.assertEqual(self.rec("Jane Doe", "secondary_directory").profile_type, "secondary")

    def test_verification_attempted_at_is_set(self):
        rerun(self.db, fetch_impl=fake_fetch({"https://a.test/jane": (200, "ok"), "https://b.test/jane": (200, "ok"), "https://a.test/john": (200, "ok")}), sleep=lambda s: None, now_fn=self.clock)
        r = self.rec("Jane Doe", "primary_directory")
        self.assertIsNotNone(r.verification_attempted_at)


class ExistingRecordRegressionTest(unittest.TestCase):
    """
    Section 11 / 6: this is the core bug fix. An existing record must be
    genuinely re-evaluated by ReRun, not preserved just because it exists.
    """

    def setUp(self):
        self.db = Storage(":memory:")
        self.clock = Clock()
        import_csv(self.db, write_csv(CSV_V1), now_fn=self.clock)

    def rec(self):
        return next(r for r in self.db.list_records() if r.url == "https://a.test/jane")

    def _rerun_jane(self, code, body="x"):
        routes = {"https://a.test/jane": (code, body), "https://b.test/jane": (200, "ok"), "https://a.test/john": (200, "ok")}
        rerun(self.db, fetch_impl=fake_fetch(routes), sleep=lambda s: None, now_fn=self.clock)

    def test_existing_active_rerun_404_becomes_inactive(self):
        self.assertEqual(self.rec().status, "active")
        self._rerun_jane(404)
        r = self.rec()
        self.assertEqual((r.url, r.created_status_code, r.updated_status_code, r.status), ("https://a.test/jane", 200, 404, "inactive"))

    def test_existing_active_rerun_403_becomes_suspect(self):
        self._rerun_jane(403)
        self.assertEqual(self.rec().status, "suspect")

    def test_existing_active_rerun_503_becomes_suspect(self):
        self._rerun_jane(503)
        self.assertEqual(self.rec().status, "suspect")

    def test_existing_inactive_rerun_200_becomes_active(self):
        self._rerun_jane(404)
        self.assertEqual(self.rec().status, "inactive")
        self._rerun_jane(200, "ok")
        r = self.rec()
        self.assertEqual((r.status, r.updated_status_code), ("active", 200))

    def test_existing_suspect_rerun_200_becomes_active(self):
        self._rerun_jane(500)
        self.assertEqual(self.rec().status, "suspect")
        self._rerun_jane(200, "ok")
        self.assertEqual(self.rec().status, "active")

    def test_existing_suspect_rerun_404_becomes_inactive(self):
        self._rerun_jane(500)
        self.assertEqual(self.rec().status, "suspect")
        self._rerun_jane(404)
        self.assertEqual(self.rec().status, "inactive")

    def test_a_second_run_import_does_not_reset_a_rerun_result(self):
        """Run must never touch verification fields of a record that already exists."""
        self._rerun_jane(404)
        self.assertEqual(self.rec().status, "inactive")
        import_csv(self.db, write_csv(CSV_V1), now_fn=self.clock)  # Run again
        r = self.rec()
        self.assertEqual((r.status, r.updated_status_code, r.created_status_code), ("inactive", 404, 200))


class CsvEditingScenariosTest(unittest.TestCase):
    def setUp(self):
        self.db = Storage(":memory:")
        self.clock = Clock()

    def test_url_edited_in_csv_retires_old_record(self):
        import_csv(self.db, write_csv(CSV_V1), now_fn=self.clock)
        edited = CSV_V1.replace("https://a.test/jane", "https://a.test/jane-updated")
        import_csv(self.db, write_csv(edited), now_fn=self.clock)

        old = next(r for r in self.db.list_records() if r.url == "https://a.test/jane")
        new = next(r for r in self.db.list_records() if r.url == "https://a.test/jane-updated")
        self.assertFalse(old.tracked)
        self.assertEqual(old.superseded_by, new.record_id)
        self.assertEqual((new.tracked, new.status, new.created_status_code), (True, "active", 200))

    def test_row_removed_is_flagged_untracked(self):
        import_csv(self.db, write_csv(CSV_V1), now_fn=self.clock)
        without_john = "name,source_name,role,url\nJane Doe,primary_directory,primary,https://a.test/jane\nJane Doe,secondary_directory,secondary,https://b.test/jane\n"
        import_csv(self.db, write_csv(without_john), now_fn=self.clock)
        john = next(r for r in self.db.list_records() if r.name == "John Doe")
        self.assertFalse(john.tracked)

    def test_ambiguous_csv_rejected(self):
        bad = "name,source_name,role,url\nJane Doe,primary_directory,primary,https://a.test/jane\nJane Doe,primary_directory,primary,https://a.test/jane-2\n"
        with self.assertRaisesRegex(CsvFormatError, "already has a different URL"):
            import_csv(self.db, write_csv(bad), now_fn=self.clock)


if __name__ == "__main__":
    unittest.main()
