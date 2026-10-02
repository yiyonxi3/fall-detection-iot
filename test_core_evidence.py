"""Isolated evidence/health tests. Run with --db-dir pointing to a test directory."""
import argparse
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
BASE_TIME = datetime(2026,10,2,12,tzinfo=timezone.utc)


class CoreEvidenceTests(unittest.TestCase):
    def setUp(self):
        global main_module
        if RUN_DIRECTORY is None:
            self.skipTest("Run explicitly with --db-dir; never use the project database")
        self.path = RUN_DIRECTORY / (uuid.uuid4().hex + ".db")
        assert not self.path.exists()
        os.environ["FALL_DB_PATH"] = str(self.path)
        os.environ["FALL_API_KEY"] = "local-test-key"
        main_module = importlib.import_module("main") if main_module is None else importlib.reload(main_module)
        self.main = main_module
        from fastapi.testclient import TestClient
        self.client = TestClient(self.main.app)
        self.headers = {"X-API-Key":"local-test-key"}

    def send(self, sequence, uptime, state="NORMAL", score=0.1, *, at=None, device="test-nano", session="A", **changes):
        data = {"device_id":device,"session_id":session,"sequence":sequence,"uptime_ms":uptime,
            "model_version":"belt_transfer_C_v2","pipeline_version":"test","threshold":0.65,
            "fall_probability":None if state == "INVALID" else score,"state":state,"is_test":True} | changes
        stamp = (BASE_TIME+timedelta(seconds=at if at is not None else uptime/1000)).isoformat()
        with patch.object(self.main,"now",return_value=stamp):
            return self.client.post("/api/v1/telemetry",headers=self.headers,json=data)

    def latest(self, at, device="test-nano"):
        with patch.object(self.main,"now",return_value=(BASE_TIME+timedelta(seconds=at)).isoformat()):
            return self.client.get(f"/api/v1/devices/{device}/latest").json()

    def evidence(self, alert=1):
        return self.client.get(f"/api/v1/alerts/{alert}/evidence").json()["evidence"]

    def test_evidence_device_time_order_and_no_fabricated_imu(self):
        self.send(10,10000)
        self.send(20,20000,"NEW_ALARM",0.9)
        initial = self.evidence()
        self.send(21,21000,"POSITIVE",0.94)
        self.send(15,15000,at=22)  # Delayed pre-trigger report arrives later.
        self.send(22,22000,"INVALID",at=23)
        self.send(24,24000,at=24,threshold=0.7)
        self.send(25,25000,"NEW_ALARM",0.97)
        self.send(1,20000,"NEW_ALARM",0.99,device="different")
        self.send(1,20000,"NEW_ALARM",0.99,session="B")
        result = self.evidence()
        self.assertEqual(result["event_key"], initial["event_key"])
        self.assertGreater(result["revision"], initial["revision"])
        self.assertEqual([p["sequence"] for p in result["points"]],[10,15,20,21,22,24,25])
        self.assertEqual(result["positive_report_count"],3)
        self.assertEqual(result["peak_received_score"],0.97)
        self.assertEqual(result["positive_observed_span_ms"],5000)
        self.assertEqual(result["related_alert_ids"],[1,2])
        self.assertEqual(result["assessment"],"CANDIDATE_ONLY")
        self.assertFalse(result["raw_imu_available"])
        self.assertIn("OUT_OF_ORDER_RECEIPT",result["warnings"])
        self.assertIn("INVALID_REPORT_IN_SPAN",result["warnings"])
        self.assertIn("CONFIGURATION_CHANGED_IN_SPAN",result["warnings"])
        self.assertEqual(result["first_post_trigger_state"],"POSITIVE")
        self.assertGreater(result["omitted_sequence_count"],0)
        self.assertNotIn("lost_packet_count",result)
        self.send(40,40000,session="A")  # Beyond bounded span still closes observation horizon.
        self.assertTrue(self.evidence()["post_span_reached"])
        self.assertEqual(len(self.evidence()["points"]),7)

    def test_duplicates_do_not_refresh_evidence_or_heartbeat(self):
        self.send(1,1000,"NEW_ALARM",0.9,at=0)
        before=self.evidence()
        response=self.send(1,1000,"NEW_ALARM",0.9,at=60)
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.evidence(),before)
        self.assertEqual(self.latest(60)["monitoring"]["status"],"UNAVAILABLE")

    def test_reporting_invalid_recovery_and_no_safety_claim(self):
        self.send(1,1000,at=0)
        self.assertEqual(self.latest(0)["monitoring"]["status"],"WARMING_UP")
        self.send(6,6000,at=5)
        self.assertEqual(self.latest(5)["monitoring"]["status"],"REPORTING")
        self.assertEqual(self.latest(5)["monitoring"]["sampling_health"],"UNVERIFIED")
        self.assertEqual(self.latest(5)["person_status"],"UNKNOWN")
        self.send(7,6000,"INVALID",at=6)
        self.assertEqual(self.latest(6)["monitoring"]["status"],"INVALID")
        self.send(8,8000,at=7)
        self.assertEqual(self.latest(7)["monitoring"]["status"],"REPORTING")
        self.assertEqual(self.latest(38)["monitoring"]["status"],"UNAVAILABLE")
        self.assertEqual(self.latest(38)["detection_result"],"NORMAL")
        self.assertEqual(self.latest(38)["person_status"],"UNKNOWN")

    def test_fresh_receipt_without_time_progress_is_not_healthy(self):
        self.send(1,1000,at=0)
        self.send(2,1000,at=35)
        result=self.latest(35)
        self.assertEqual(result["connectivity"],"ONLINE")
        self.assertEqual(result["monitoring"]["status"],"PROGRESS_UNVERIFIED")

    def test_relative_lag_warning_and_catchup(self):
        self.send(1,1000,at=0)
        self.send(6,6000,at=45)
        self.assertEqual(self.latest(45)["monitoring"]["status"],"DELAY_SUSPECTED")
        self.assertEqual(self.latest(45)["monitoring"]["relative_lag_growth_ms"],40000)
        self.send(47,47000,at=46)
        self.assertEqual(self.latest(46)["monitoring"]["status"],"REPORTING")

    def test_old_session_and_out_of_order_cannot_overwrite_new_state(self):
        self.send(1,1000,at=0)
        self.send(10,10000,at=9)
        self.send(2,2000,"NEW_ALARM",0.9,at=10)
        self.assertEqual(self.latest(10)["state"],"NORMAL")
        self.send(1,1000,at=20,session="B")
        self.send(2,2000,at=21,session="B")
        self.send(20,20000,"NEW_ALARM",0.99,at=22,session="A")
        latest=self.latest(22)
        self.assertEqual(latest["session_id"],"B")
        self.assertEqual(latest["state"],"NORMAL")
        self.assertIn("SESSION_CHANGE_OBSERVED",latest["monitoring"]["reasons"])
        self.assertEqual(latest["monitoring"]["session_count"],2)
        self.send(30,30000,at=60,session="A")
        self.assertEqual(self.latest(60)["monitoring"]["status"],"UNAVAILABLE")

    def test_restart_stability_and_atomic_ingest(self):
        self.send(1,1000,"NEW_ALARM",0.9,at=0)
        before=self.evidence()
        self.main.initialise_database()
        self.assertEqual(self.evidence(),before)
        with patch.object(self.main.event_evidence,"ingest",side_effect=RuntimeError("test failure")):
            with self.assertRaises(RuntimeError): self.send(2,2000,"NEW_ALARM",0.8)
        self.assertEqual(len(self.client.get("/api/v1/events").json()),1)
        self.assertEqual(len(self.client.get("/api/v1/alerts").json()),1)
        self.assertEqual(self.evidence(),before)
        global main_module
        main_module=importlib.reload(self.main)
        from fastapi.testclient import TestClient
        self.client=TestClient(main_module.app)
        self.assertEqual(self.evidence(),before)

    def test_dense_evidence_cap_retains_trigger(self):
        for index in range(205):
            self.send(index+1,1000+index*20,"NEW_ALARM" if index == 100 else "NORMAL",
                      0.9 if index == 100 else 0.1)
        result=self.evidence()
        self.assertEqual(result["received_point_count"],205)
        self.assertEqual(result["returned_point_count"],200)
        self.assertEqual(sum(point["is_trigger"] for point in result["points"]),1)
        self.assertIn("EVIDENCE_POINTS_TRUNCATED",result["warnings"])

    def test_v07_backfill_preserves_notes_and_operations(self):
        # Start from a genuine committed v0.7 initializer and human workflow, in a new DB.
        old_path=RUN_DIRECTORY/(uuid.uuid4().hex+"-v07.db")
        os.environ["FALL_DB_PATH"]=str(old_path)
        source=subprocess.check_output(["git","show","b951a73:main.py"],cwd=Path(__file__).parent).decode("utf-8")
        old={"__file__":str(Path(__file__).with_name("main.py")),"__name__":"old_v07_test"}
        exec(compile(source,"committed_v07.py","exec"),old)
        from fastapi import Response
        old["receive_telemetry"](old["TelemetryRequest"](device_id="legacy",session_id="old-session",
            sequence=1,uptime_ms=5000,model_version="belt_transfer_C_v2",pipeline_version="test",
            threshold=0.65,state="NEW_ALARM",fall_probability=0.9,is_test=True),Response())
        old["set_review_status"](1,True,"member-legacy")
        old["update_note"](1,old["NoteRequest"](category="OTHER",note="历史备注",expected_revision=0,updated_by="member-legacy"))
        with sqlite3.connect(old_path) as conn:
            conn.row_factory=sqlite3.Row
            before=dict(conn.execute("SELECT * FROM alerts").fetchone())
            operations=[dict(row) for row in conn.execute("SELECT * FROM alert_operations")]
        global main_module
        main_module=importlib.reload(self.main)
        from fastapi.testclient import TestClient
        client=TestClient(main_module.app)
        context=client.get("/api/v1/alerts/1/context").json()
        self.assertTrue(all(context["alert"][key] == value for key,value in before.items()))
        self.assertEqual(context["operations_total"],2)
        self.assertEqual(context["evidence"]["source"],"reconstructed")
        with sqlite3.connect(old_path) as conn:
            conn.row_factory=sqlite3.Row
            self.assertEqual([dict(row) for row in conn.execute("SELECT * FROM alert_operations")],operations)
        stable=context["evidence"]
        main_module.initialise_database()
        self.assertEqual(client.get("/api/v1/alerts/1/context").json()["evidence"],stable)

    def test_missing_evidence_and_read_only_access(self):
        self.assertEqual(self.client.get("/api/v1/alerts/999/evidence").status_code,404)
        self.send(1,5000,"NEW_ALARM",0.9)
        self.assertEqual(self.client.get("/api/v1/alerts/1/evidence").status_code,200)
        self.assertFalse(self.evidence()["raw_imu_available"])
        self.assertIn("NO_POST_TRIGGER_VALID_REPORT",self.evidence()["warnings"])


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--db-dir",type=Path,required=True,help="Directory for isolated test SQLite files; never the production database")
    args=parser.parse_args()
    RUN_DIRECTORY=args.db_dir.resolve()
    RUN_DIRECTORY.mkdir(parents=True,exist_ok=True)
    unittest.main(argv=[__file__],verbosity=2)
