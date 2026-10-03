"""Incident integration tests; require an explicit directory for fresh isolated DBs."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import importlib
import os
from pathlib import Path
import sqlite3
import subprocess
import unittest
from unittest.mock import patch
import uuid

RUN_DIRECTORY = None
main_module = None
BASE = datetime(2026, 10, 3, tzinfo=timezone.utc)


class IncidentTests(unittest.TestCase):
    def setUp(self):
        global main_module
        if RUN_DIRECTORY is None:
            self.skipTest("Use --db-dir; never use the project database")
        self.path = RUN_DIRECTORY / (uuid.uuid4().hex + ".db")
        os.environ["FALL_DB_PATH"] = str(self.path)
        os.environ["FALL_API_KEY"] = "local-test-key"
        main_module = importlib.import_module("main") if main_module is None else importlib.reload(main_module)
        self.main = main_module
        from fastapi.testclient import TestClient
        self.client = TestClient(self.main.app)
        self.headers = {"X-API-Key": "local-test-key"}

    def stamp(self, seconds):
        return (BASE + timedelta(seconds=seconds)).isoformat()

    def send(self, seq, seconds, state="NEW_ALARM", score=0.9, at=None, **changes):
        data = dict(device_id="replay", session_id="A", sequence=seq, uptime_ms=int(seconds*1000),
                    model_version="belt_transfer_C_v2", pipeline_version="test", threshold=0.65,
                    fall_probability=None if state == "INVALID" else score, state=state, is_test=True)
        data.update(changes)
        with patch.object(self.main, "now", return_value=self.stamp(seconds if at is None else at)):
            return self.client.post("/api/v1/telemetry", headers=self.headers, json=data)

    def detail(self, ident=1, at=15):
        with patch.object(self.main, "now", return_value=self.stamp(at)):
            return self.client.get(f"/api/v1/incidents/{ident}")

    def complete(self):
        self.send(1, 1)
        self.send(2, 3, score=0.99)
        self.send(3, 8, "NORMAL", 0.1)
        self.send(4, 13, "NORMAL", 0.1)

    def test_multiple_candidates_and_retained_originals(self):
        self.complete()
        item = self.detail().json()
        self.assertEqual(item["candidate_count"], 2)
        self.assertEqual(item["model_state"], "MULTIPLE_CANDIDATES")
        self.assertEqual(item["peak_score"], 0.99)
        self.assertEqual(item["evidence"]["state"], "OBSERVED")
        self.assertEqual(item["evidence"]["first_post_candidate_report"]["state"], "NORMAL")
        self.assertEqual(item["assessment"], "SUSPECTED_EVENT_ONLY")
        self.assertEqual(len(self.client.get("/api/v1/alerts").json()), 2)
        self.assertEqual(item["candidates"][1]["relation_reason"], "SAME_SESSION_CONFIG_WITHIN_ANCHOR_10S")

    def test_fixed_anchor_boundary_not_sliding(self):
        self.send(1, 1); self.send(2, 10); self.send(3, 19)
        items = self.client.get("/api/v1/incidents").json()
        self.assertEqual([item["candidate_count"] for item in items], [1, 2])

    def test_anchor_window_inclusive_and_just_outside(self):
        self.send(1, 1); self.send(2, 11); self.send(3, 11.001)
        self.assertEqual(self.detail().json()["candidate_count"], 2)
        self.assertEqual(self.detail(2).json()["candidate_count"], 1)

    def test_device_session_and_configuration_boundaries(self):
        self.send(1, 1)
        self.send(2, 2, device_id="other")
        self.send(1, 2, session_id="B")
        self.send(3, 3, session_id="A")  # Previously known session after a newer one.
        self.send(4, 4, threshold=0.7)
        self.assertEqual(len(self.client.get("/api/v1/incidents").json()), 5)
        self.assertIn("SESSION_CHANGED_BEFORE_HORIZON", self.detail().json()["evidence"]["reasons"])

    def test_intervening_configuration_change_splits(self):
        self.send(1, 1)
        self.send(2, 2, "NORMAL", 0.1, pipeline_version="changed")
        self.send(3, 3)
        self.assertEqual(len(self.client.get("/api/v1/incidents").json()), 2)
        self.assertEqual(self.detail(2).json()["candidates"][0]["relation_reason"], "CONFIGURATION_BOUNDARY")

    def test_duplicate_and_positive_do_not_add_members(self):
        self.send(1, 1)
        before = self.detail(at=1).json()
        self.assertEqual(self.send(1, 1, at=40).status_code, 200)
        self.assertEqual(self.detail(at=1).json(), before)
        self.send(2, 2, "POSITIVE", 0.95)
        self.assertEqual(self.detail().json()["candidate_count"], 1)
        self.assertEqual(len(self.client.get("/api/v1/alerts").json()), 1)

    def test_public_timeout_read_has_no_writes(self):
        self.send(1, 1)
        with closing(sqlite3.connect(self.path)) as conn:
            before = conn.execute("SELECT * FROM incidents").fetchall()
        item = self.detail(at=32).json()
        self.assertEqual(item["evidence"]["state"], "INCOMPLETE")
        self.assertIn("POST_REPORT_TIMEOUT", item["evidence"]["reasons"])
        self.assertTrue(item["requires_evidence_check"])
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT * FROM incidents").fetchall(), before)
        self.assertEqual(self.detail(999).status_code, 404)

    def test_receipt_without_device_progress_cannot_complete(self):
        self.send(1, 1)
        self.send(2, 1, "NORMAL", 0.1, at=10)
        self.send(3, 1, "NORMAL", 0.1, at=33)
        item = self.detail(at=33).json()
        self.assertEqual(item["evidence"]["state"], "INCOMPLETE")
        self.assertFalse(item["evidence"]["post_horizon_reached"])

    def test_invalid_and_sparse_evidence_not_called_complete(self):
        self.send(1, 1)
        self.send(2, 2, "INVALID")
        self.send(3, 6, "NORMAL", 0.1)
        self.send(4, 11, "NORMAL", 0.1)
        item = self.detail().json()
        self.assertTrue(item["evidence"]["post_horizon_reached"])
        self.assertEqual(item["evidence"]["state"], "INCOMPLETE")
        self.assertIn("INVALID_REPORT", item["evidence"]["reasons"])
        self.send(5, 50)
        self.send(6, 60, "NORMAL", 0.1)
        self.assertIn("RECEIVED_REPORT_GAP", self.detail(2, 60).json()["evidence"]["reasons"])

    def test_completed_historical_evidence_survives_later_outage(self):
        self.complete()
        before = self.detail().json()["evidence"]
        after = self.detail(at=100).json()
        self.assertEqual(after["evidence"], before)
        self.assertEqual(after["current_monitoring"]["status"], "UNAVAILABLE")
        self.send(5, 100, "NORMAL", 0.1)
        self.assertEqual(self.detail(at=100).json()["evidence"], before)

    def test_late_pre_anchor_never_rewrites_event_identity(self):
        self.send(3, 3)
        self.send(1, 1, at=4)
        self.send(4, 4, at=5)
        self.assertEqual(self.detail().json()["candidate_count"], 2)
        self.assertEqual(self.detail(2).json()["candidate_count"], 1)
        self.assertEqual(self.detail(2).json()["candidates"][0]["relation_reason"], "LATE_OUT_OF_ORDER_CANDIDATE")
        self.assertEqual(self.detail().json()["first_alert_id"], 1)

    def test_accumulating_relative_delay_flagged(self):
        self.send(1, 1)
        self.send(2, 6, "NORMAL", 0.1, at=21)
        self.send(3, 11, "NORMAL", 0.1, at=42)
        item = self.detail(at=42).json()
        self.assertEqual(item["evidence"]["state"], "INCOMPLETE")
        self.assertIn("RELATIVE_DELAY_SUSPECTED", item["evidence"]["reasons"])

    def test_long_arrival_interruption_not_hidden_by_association(self):
        self.send(1, 1)
        self.send(2, 2, at=40)
        self.assertEqual(len(self.client.get("/api/v1/incidents").json()), 2)
        self.assertIn("POST_REPORT_TIMEOUT", self.detail(at=40).json()["evidence"]["reasons"])

    def test_human_workflow_filters_and_pagination(self):
        self.complete()
        self.send(5, 25, is_test=False)
        self.assertEqual(len(self.client.get("/api/v1/incidents?hide_tests=true").json()), 1)
        page = self.client.get("/api/v1/incidents?limit=1").json()
        self.assertEqual(page[0]["id"], 2)
        self.assertEqual(self.client.get("/api/v1/incidents?limit=1&before_id=2").json()[0]["id"], 1)
        body = dict(confirmed=True, updated_by="member-01")
        self.assertEqual(self.client.patch("/api/v1/alerts/1/review", json=body).status_code, 401)
        self.client.patch("/api/v1/alerts/1/review", headers=self.headers, json=body)
        self.assertEqual(self.detail().json()["reviewed_count"], 1)
        self.client.patch("/api/v1/alerts/2/review", headers=self.headers, json=body)
        self.client.patch("/api/v1/alerts/1/note", headers=self.headers,
            json=dict(category="OTHER", note="回放样本", expected_revision=0, updated_by="member-01"))
        self.assertEqual(self.detail().json()["review_state"], "REVIEWED")
        self.assertEqual(self.detail().json()["candidates"][0]["judgment_note"], "回放样本")
        self.assertEqual(len(self.client.get("/api/v1/incidents?open_only=true").json()), 1)
        self.assertEqual(self.client.get("/api/v1/alerts/1/context").json()["operations_total"], 2)
        self.client.patch("/api/v1/alerts/1/review", headers=self.headers,
                          json=dict(confirmed=False, updated_by="member-01"))
        self.assertEqual(self.detail().json()["review_state"], "PENDING")

    def test_restart_and_atomic_rollback(self):
        self.complete()
        before = self.detail().json()
        self.main.initialise_database()
        self.assertEqual(self.detail().json(), before)
        with patch.object(self.main.incident_engine, "ingest", side_effect=RuntimeError("isolated failure")):
            with self.assertRaises(RuntimeError):
                self.send(5, 14)
        self.assertEqual(len(self.client.get("/api/v1/events").json()), 4)
        self.assertEqual(self.detail().json(), before)
        global main_module
        main_module = importlib.reload(self.main)
        from fastapi.testclient import TestClient
        self.client = TestClient(main_module.app)
        self.assertEqual(self.detail().json(), before)

    def test_genuine_v08_migration_preserves_data_and_human_history(self):
        legacy = RUN_DIRECTORY / (uuid.uuid4().hex + "-v08.db")
        os.environ["FALL_DB_PATH"] = str(legacy)
        source = subprocess.check_output(["git", "show", "5772825:main.py"], cwd=Path(__file__).parent).decode("utf-8")
        namespace = dict(__file__=str(Path(__file__).with_name("main.py")), __name__="legacy_v08")
        exec(compile(source, "legacy_v08.py", "exec"), namespace)
        from fastapi import Response
        data = dict(device_id="legacy", session_id="old", sequence=1, uptime_ms=1000,
                    model_version="belt_transfer_C_v2", pipeline_version="test", threshold=0.65,
                    fall_probability=0.9, state="NEW_ALARM", is_test=True)
        namespace["receive_telemetry"](namespace["TelemetryRequest"](**data), Response())
        namespace["set_review_status"](1, True, "member-legacy")
        namespace["update_note"](1, namespace["NoteRequest"](category="OTHER", note="保留历史",
                                      expected_revision=0, updated_by="member-legacy"))
        tables = ("telemetry_events", "alerts", "alert_operations", "event_evidence")
        with closing(sqlite3.connect(legacy)) as conn:
            before = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
            old_columns = {table: [row[1] for row in conn.execute(f"PRAGMA table_info({table})")] for table in tables}
        global main_module
        main_module = importlib.reload(self.main)
        from fastapi.testclient import TestClient
        self.client = TestClient(main_module.app)
        item = self.detail().json()
        self.assertEqual(item["source"], "reconstructed")
        self.assertEqual(item["candidates"][0]["judgment_note"], "保留历史")
        with closing(sqlite3.connect(legacy)) as conn:
            self.assertEqual({table: conn.execute(f"SELECT {','.join(old_columns[table])} FROM {table}").fetchall() for table in tables}, before)
            self.assertEqual(conn.execute("SELECT processing_complete,processed_at,processed_by FROM alerts").fetchall(), [(0,None,None)])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-dir", type=Path, required=True)
    args = parser.parse_args()
    RUN_DIRECTORY = args.db_dir.resolve()
    RUN_DIRECTORY.mkdir(parents=True, exist_ok=True)
    unittest.main(argv=[__file__], verbosity=2)
