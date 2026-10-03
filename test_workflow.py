"""Human workflow regression using fresh isolated databases, never the project DB."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import importlib
import json
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


class WorkflowTests(unittest.TestCase):
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
        self.send(1, 1)
        self.send(2, 3)

    def send(self, sequence, seconds, **changes):
        data = dict(device_id="workflow-demo", session_id="A", sequence=sequence, uptime_ms=seconds*1000,
                    model_version="belt_transfer_C_v2", pipeline_version="test", threshold=.65,
                    fall_probability=.9, state="NEW_ALARM", is_test=True)
        data.update(changes)
        with patch.object(self.main, "now", return_value=(BASE+timedelta(seconds=seconds)).isoformat()):
            return self.client.post("/api/v1/telemetry", json=data, headers=self.headers)

    def detail(self, ident=1):
        return self.client.get(f"/api/v1/incidents/{ident}").json()

    def body(self, operation, ids=None, **changes):
        item = self.detail()
        data = dict(operation=operation, alert_ids=ids or [1, 2],
                    expected_member_ids=[row["id"] for row in item["candidates"]],
                    expected_revision=item["workflow_revision"], updated_by="member-test")
        if operation in {"SAVE", "FINISH"}:
            data.update(category="SUSPECTED_FALSE_POSITIVE", note="已询问组员，当时正在坐下")
        data.update(changes)
        return data

    def update(self, operation, ids=None, **changes):
        return self.client.patch("/api/v1/incidents/1/workflow", headers=self.headers,
                                 json=self.body(operation, ids, **changes))

    def dump(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return list(conn.iterdump())

    def test_review_is_not_completion_and_open_does_not_write(self):
        before = self.dump()
        self.assertEqual(self.detail()["workflow_state"], "UNREAD")
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.update("REVIEW").status_code, 200)
        item = self.detail()
        self.assertEqual(item["workflow_state"], "VERIFY")
        self.assertEqual(item["completed_count"], 0)
        self.assertEqual(item["reviewed_count"], 2)

    def test_finish_requires_review_result_and_note_without_partial_save(self):
        before = self.dump()
        self.assertEqual(self.update("FINISH").json()["detail"], "REVIEW_REQUIRED")
        self.assertEqual(self.dump(), before)
        self.update("REVIEW")
        for category, note, reason in [("UNASSESSED", "x", "VERIFICATION_REQUIRED"),
                                        ("NEEDS_VERIFICATION", "x", "VERIFICATION_REQUIRED"),
                                        ("OTHER", "  ", "NOTE_REQUIRED")]:
            before = self.dump()
            result = self.update("FINISH", category=category, note=note)
            self.assertEqual(result.status_code, 422)
            self.assertEqual(result.json()["detail"], reason)
            self.assertEqual(self.dump(), before)

    def test_save_progress_does_not_review_or_finish(self):
        self.assertEqual(self.update("SAVE", category="NEEDS_VERIFICATION").status_code, 200)
        item = self.detail()
        self.assertEqual(item["workflow_state"], "UNREAD")
        self.assertTrue(all(not row["processing_complete"] for row in item["candidates"]))

    def test_selected_record_only_and_explicit_all_finish(self):
        self.update("REVIEW")
        self.update("FINISH", [1])
        item = self.detail()
        self.assertEqual(item["workflow_state"], "VERIFY")
        self.assertEqual(item["completed_count"], 1)
        self.assertEqual(item["candidates"][1]["judgment_category"], "UNASSESSED")
        self.update("FINISH", [2], category="SIMULATED_TEST", note="另一次模拟测试")
        self.assertEqual(self.detail()["workflow_state"], "COMPLETED")

    def test_reopen_keeps_notes_and_completion_history(self):
        self.update("REVIEW"); self.update("FINISH")
        before = self.detail()
        self.update("REOPEN", [2])
        item = self.detail()
        self.assertEqual(item["workflow_state"], "VERIFY")
        self.assertEqual(item["candidates"][1]["judgment_note"], before["candidates"][1]["judgment_note"])
        history = item["workflow_operations"]["items"][0]
        self.assertEqual(history["action"], "REOPEN")
        self.assertEqual(history["selected_alert_ids"], [2])
        self.assertTrue(history["before"]["records"][0]["processing_complete"])
        self.assertFalse(history["after"]["records"][0]["processing_complete"])

    def test_alarm_handling_required_and_new_marker_clears_old_handling(self):
        self.update("REVIEW"); self.update("ALARM_MARK", [1])
        self.assertEqual(self.update("FINISH").json()["detail"], "HANDLING_REQUIRED")
        self.assertEqual(self.update("FINISH", handling_note="已联系组员并完成记录").status_code, 200)
        self.assertEqual(self.detail()["workflow_state"], "COMPLETED")
        self.update("ALARM_MARK", [2])
        item = self.detail()
        self.assertEqual(item["handling_note"], "")
        self.assertFalse(item["candidates"][1]["processing_complete"])
        self.assertEqual(self.update("FINISH", [2]).json()["detail"], "HANDLING_REQUIRED")

    def test_atomic_revision_conflict_and_late_member(self):
        stale = self.body("SAVE")
        self.update("REVIEW", [1])
        before = self.dump()
        self.assertEqual(self.client.patch("/api/v1/incidents/1/workflow", headers=self.headers, json=stale).status_code, 409)
        self.assertEqual(self.dump(), before)
        stale = self.body("SAVE")
        self.send(3, 5)
        before = self.dump()
        self.assertEqual(self.client.patch("/api/v1/incidents/1/workflow", headers=self.headers, json=stale).status_code, 409)
        self.assertEqual(self.dump(), before)

    def test_cross_incident_selection_and_validation(self):
        self.send(1, 2, device_id="other")
        for changes in [{"alert_ids": [1, 3]}, {"alert_ids": [1, 1]}, {"alert_ids": []},
                        {"alert_ids": [True]}, {"expected_revision": True}, {"updated_by": " "},
                        {"note": "x"*1001}]:
            before = self.dump()
            body = self.body("SAVE", **changes)
            self.assertEqual(self.client.patch("/api/v1/incidents/1/workflow", headers=self.headers, json=body).status_code, 422)
            self.assertEqual(self.dump(), before)

    def test_authenticated_writes_and_public_reads(self):
        before = self.dump()
        self.assertEqual(self.client.patch("/api/v1/incidents/1/workflow", json=self.body("REVIEW")).status_code, 401)
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.client.get("/api/v1/incidents/1").status_code, 200)
        self.assertEqual(self.client.patch("/api/v1/incidents/999/workflow", headers=self.headers, json=self.body("REVIEW")).status_code, 404)

    def test_idempotent_same_state_does_not_duplicate_operations(self):
        self.update("REVIEW")
        before = self.dump()
        self.assertEqual(self.update("REVIEW").json()["status"], "unchanged")
        self.assertEqual(self.dump(), before)
        self.update("FINISH")
        before = self.dump()
        self.assertEqual(self.update("FINISH").json()["status"], "unchanged")
        self.assertEqual(self.dump(), before)

    def test_legacy_changes_conflict_and_reopen_completed_processing(self):
        self.update("REVIEW"); self.update("FINISH")
        stale = self.body("FINISH")
        row = self.detail()["candidates"][0]
        self.client.patch("/api/v1/alerts/1/note", headers=self.headers,
                          json=dict(category="OTHER", note="新说明", expected_revision=row["note_revision"], updated_by="legacy-member"))
        self.assertEqual(self.detail()["workflow_state"], "VERIFY")
        self.assertEqual(self.client.patch("/api/v1/incidents/1/workflow", headers=self.headers, json=stale).status_code, 409)
        self.update("FINISH", [1], category="OTHER", note="新说明")
        self.client.patch("/api/v1/alerts/1/review", headers=self.headers, json=dict(confirmed=False, updated_by="legacy-member"))
        self.assertEqual(self.detail()["workflow_state"], "UNREAD")
        self.assertFalse(self.detail()["candidates"][0]["processing_complete"])

    def test_new_workflow_requires_reopen_before_unreview(self):
        self.update("REVIEW"); self.update("FINISH")
        self.assertEqual(self.update("UNREVIEW").json()["detail"], "REOPEN_REQUIRED")

    def test_transaction_rolls_back_all_changes_if_history_write_fails(self):
        before = self.dump()
        original = self.main.record_operation
        calls = 0
        def fail_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected history failure")
            return original(*args)
        with patch.object(self.main, "record_operation", side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                self.update("REVIEW")
        self.assertEqual(self.dump(), before)

    def test_concurrent_same_revision_has_one_winner(self):
        body = self.body("SAVE")
        def request(_):
            return self.client.patch("/api/v1/incidents/1/workflow", headers=self.headers, json=body).status_code
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(request, range(2))), [200, 409])

    def test_filter_composition_and_completed_late_candidate_reopens(self):
        self.update("REVIEW"); self.update("FINISH")
        self.send(1, 2, device_id="other", is_test=False)
        self.assertEqual(len(self.client.get("/api/v1/incidents?workflow_state=COMPLETED").json()), 1)
        self.assertEqual(self.client.get("/api/v1/incidents?workflow_state=PENDING&device_id=workflow-demo").json(), [])
        self.assertEqual(len(self.client.get("/api/v1/incidents?workflow_state=PENDING&hide_tests=true").json()), 1)
        self.send(3, 5)
        self.assertEqual(self.detail()["workflow_state"], "UNREAD")
        self.assertEqual(self.detail()["completed_count"], 2)

    def test_restart_preserves_completion_and_gets_are_read_only(self):
        self.update("REVIEW"); self.update("FINISH")
        before = self.dump()
        self.main.initialise_database()
        self.assertEqual(self.detail()["workflow_state"], "COMPLETED")
        self.client.get("/api/v1/incidents?workflow_state=PENDING")
        self.assertEqual(self.dump(), before)

    def test_v09_migration_preserves_old_fields_and_never_invents_completion(self):
        legacy = RUN_DIRECTORY / (uuid.uuid4().hex+".db")
        os.environ["FALL_DB_PATH"] = str(legacy)
        source = subprocess.check_output(["git", "show", "1553123:main.py"], text=True, encoding="utf-8")
        namespace = {"__file__": str(Path(self.main.__file__)), "__name__": "legacy_v09"}
        exec(compile(source, "legacy_v09.py", "exec"), namespace)
        from fastapi import Response
        namespace["receive_telemetry"](namespace["TelemetryRequest"](device_id="legacy",session_id="old",sequence=1,
            uptime_ms=1000,model_version="belt_transfer_C_v2",pipeline_version="test",threshold=.65,
            fall_probability=.9,state="NEW_ALARM",is_test=True), Response())
        namespace["set_review_status"](1,True,"legacy-member")
        namespace["update_note"](1,namespace["NoteRequest"](category="OTHER",note="保留原备注",expected_revision=0,updated_by="legacy-member"))
        with closing(sqlite3.connect(legacy)) as conn:
            conn.row_factory=sqlite3.Row
            before=dict(conn.execute("SELECT * FROM alerts WHERE id=1").fetchone())
            operations=conn.execute("SELECT * FROM alert_operations").fetchall()
        global main_module
        main_module=importlib.reload(self.main)
        from fastapi.testclient import TestClient
        client=TestClient(main_module.app)
        item=client.get("/api/v1/incidents/1").json()
        self.assertEqual(item["workflow_state"],"VERIFY")
        self.assertFalse(item["candidates"][0]["processing_complete"])
        self.assertEqual(item["workflow_operations"]["total"],0)
        with closing(sqlite3.connect(legacy)) as conn:
            conn.row_factory=sqlite3.Row
            after=dict(conn.execute("SELECT * FROM alerts WHERE id=1").fetchone())
            self.assertTrue(all(after[key]==value for key,value in before.items()))
            self.assertEqual([tuple(row) for row in conn.execute("SELECT * FROM alert_operations")],[tuple(row) for row in operations])


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--db-dir",type=Path,required=True)
    args=parser.parse_args()
    RUN_DIRECTORY=args.db_dir.resolve()
    RUN_DIRECTORY.mkdir(parents=True,exist_ok=True)
    unittest.main(argv=[__file__],verbosity=2)
