"""Дедуп заявок и аудит-журнал."""
from __future__ import annotations

import sqlite3
import time
from decimal import Decimal

from config import settings
from services import audit, dedup
from tests.conftest import make_request


class TestDedup:
    def test_fingerprint_normalizes_case_and_spaces(self, tmp_paths):
        a = dedup.fingerprint(make_request(counterparty="ООО Ромашка"))
        b = dedup.fingerprint(make_request(counterparty="  ооо   РОМАШКА "))
        assert a == b

    def test_fingerprint_sensitive_to_key_fields(self, tmp_paths):
        base = dedup.fingerprint(make_request())
        assert dedup.fingerprint(make_request(amount=Decimal("125000.51"))) != base
        assert dedup.fingerprint(make_request(article="Прочее")) != base
        assert dedup.fingerprint(make_request(currency="USD")) != base

    def test_duplicate_detected_within_window(self, tmp_paths):
        r = make_request()
        assert dedup.check_sync(dedup.fingerprint(r)) is None
        dedup.remember_sync(dedup.fingerprint(r), r.request_id)
        assert dedup.check_sync(dedup.fingerprint(r)) is not None

    def test_old_entries_outside_window_ignored(self, tmp_paths):
        r = make_request()
        fp = dedup.fingerprint(r)
        dedup.remember_sync(fp, r.request_id)
        old_ts = time.time() - (settings.dedup_window_days + 1) * 86400
        with sqlite3.connect(settings.security_db_path) as conn:
            conn.execute("UPDATE dedup SET ts = ?", (old_ts,))
        assert dedup.check_sync(fp) is None

    def test_window_zero_disables_check(self, tmp_paths, monkeypatch):
        r = make_request()
        fp = dedup.fingerprint(r)
        dedup.remember_sync(fp, r.request_id)
        monkeypatch.setattr(settings, "dedup_window_days", 0)
        assert dedup.check_sync(fp) is None


class TestAudit:
    def test_events_are_recorded_and_ordered(self, tmp_paths):
        audit.log_event_sync(audit.ACCESS_DENIED, 111, "@stranger", "чат-форма")
        audit.log_event_sync(audit.REQUEST_SUBMITTED, 222, "@employee", "INV-1 · 100 RUB")
        events = audit.recent_events_sync(10)
        assert [e["event"] for e in events] == [audit.REQUEST_SUBMITTED, audit.ACCESS_DENIED]
        assert events[1]["user_id"] == 111
        assert events[1]["username"] == "@stranger"

    def test_details_are_truncated(self, tmp_paths):
        audit.log_event_sync(audit.REQUEST_FAILED, 1, None, "x" * 2000)
        assert len(audit.recent_events_sync(1)[0]["details"]) == 500

    def test_limit_respected(self, tmp_paths):
        for i in range(20):
            audit.log_event_sync(audit.REQUEST_SUBMITTED, i, None, "")
        assert len(audit.recent_events_sync(5)) == 5
