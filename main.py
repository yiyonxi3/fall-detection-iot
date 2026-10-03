"""Backend for Nano-side fall detection: receive, store, and query telemetry."""

from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sqlite3
from typing import Annotated, Literal

import event_evidence
import incident_engine
import workflow
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StringConstraints, model_validator

OperatorName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]

DEFAULT_DB_PATH = Path(__file__).with_name("fall_detection.db")
DB_PATH = Path(os.getenv("FALL_DB_PATH", str(DEFAULT_DB_PATH)))
DASHBOARD_PATH = Path(__file__).with_name("dashboard.html")
API_KEY = os.getenv("FALL_API_KEY", "local-test-key")
DEVICE_OFFLINE_SECONDS = int(os.getenv("DEVICE_OFFLINE_SECONDS", "30"))
app = FastAPI(
    title="IoT Fall Detection API",
    description="Nano performs inference; this server receives, stores and displays results.",
    version="0.10.0",
)


class TelemetryRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=100)
    session_id: str = Field(min_length=1, max_length=100)
    sequence: int = Field(ge=0)
    uptime_ms: int = Field(ge=0)
    model_version: str = Field(min_length=1, max_length=100)
    pipeline_version: str = Field(min_length=1, max_length=100)
    fall_probability: float | None = Field(default=None, ge=0, le=1)
    threshold: float = Field(ge=0, le=1)
    state: Literal["NORMAL", "NEW_ALARM", "POSITIVE", "INVALID"]
    is_test: bool = False

    @model_validator(mode="after")
    def validate_score(self):
        if self.state == "INVALID" and self.fall_probability is not None:
            raise ValueError("INVALID telemetry must set fall_probability to null")
        if self.state != "INVALID" and self.fall_probability is None:
            raise ValueError("Non-INVALID telemetry requires fall_probability")
        if self.state == "NORMAL" and self.fall_probability >= self.threshold:
            raise ValueError("NORMAL telemetry must be below threshold")
        if self.state in {"NEW_ALARM", "POSITIVE"} and self.fall_probability < self.threshold:
            raise ValueError("Positive telemetry must meet or exceed threshold")
        return self


class AcknowledgeRequest(BaseModel):
    acknowledged_by: OperatorName


class ReviewRequest(BaseModel):
    confirmed: bool = Field(strict=True)
    updated_by: OperatorName


class AlarmMarkRequest(BaseModel):
    alarm_required: bool = Field(strict=True)
    updated_by: OperatorName


class NoteRequest(BaseModel):
    category: Literal["UNASSESSED", "SIMULATED_TEST", "SUSPECTED_FALSE_POSITIVE", "NEEDS_VERIFICATION", "OTHER"]
    note: str = Field(max_length=1000)
    expected_revision: int = Field(ge=0, strict=True)
    updated_by: OperatorName


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def initialise_database() -> None:
    with closing(db_connection()) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS telemetry_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL, session_id TEXT NOT NULL, sequence INTEGER NOT NULL,
            uptime_ms INTEGER NOT NULL, model_version TEXT NOT NULL, pipeline_version TEXT NOT NULL,
            fall_probability REAL, threshold REAL NOT NULL, state TEXT NOT NULL,
            is_test INTEGER NOT NULL, received_at TEXT NOT NULL,
            UNIQUE(device_id, session_id, sequence)
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telemetry_event_id INTEGER NOT NULL UNIQUE, device_id TEXT NOT NULL,
            is_test INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN', created_at TEXT NOT NULL,
            acknowledged_at TEXT, acknowledged_by TEXT,
            alarm_required INTEGER NOT NULL DEFAULT 0 CHECK(alarm_required IN (0, 1)),
            alarm_marked_at TEXT, alarm_marked_by TEXT,
            FOREIGN KEY(telemetry_event_id) REFERENCES telemetry_events(id)
        );
        CREATE INDEX IF NOT EXISTS idx_events_device_received
            ON telemetry_events(device_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_alerts_status
            ON alerts(status, id DESC);
        """)
        conn.execute("PRAGMA journal_mode=WAL")
        # Add manual alarm fields to existing Volume databases without rebuilding tables.
        conn.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(alerts)")}
        additions = {
            "alarm_required": "INTEGER NOT NULL DEFAULT 0 CHECK(alarm_required IN (0, 1))",
            "alarm_marked_at": "TEXT",
            "alarm_marked_by": "TEXT",
            "judgment_category": "TEXT NOT NULL DEFAULT 'UNASSESSED'",
            "judgment_note": "TEXT NOT NULL DEFAULT ''",
            "note_revision": "INTEGER NOT NULL DEFAULT 0",
            "note_updated_at": "TEXT",
            "note_updated_by": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE alerts ADD COLUMN {name} {definition}")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id INTEGER NOT NULL,
                action TEXT NOT NULL, operated_at TEXT NOT NULL, operated_by TEXT NOT NULL,
                before_json TEXT NOT NULL, after_json TEXT NOT NULL,
                FOREIGN KEY(alert_id) REFERENCES alerts(id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_operations_alert ON alert_operations(alert_id, id DESC)")
        event_evidence.initialise(conn)
        incident_engine.initialise(conn, DEVICE_OFFLINE_SECONDS)
        workflow.initialise(conn)
        conn.commit()


def to_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    if "is_test" in item:
        item["is_test"] = bool(item["is_test"])
    if "alarm_required" in item:
        item["alarm_required"] = bool(item["alarm_required"])
    if "processing_complete" in item:
        item["processing_complete"] = bool(item["processing_complete"])
    return item


def device_status(row: sqlite3.Row, monitoring: dict | None = None) -> dict:
    """Separate connection freshness from the model's last detection result."""
    item = to_dict(row)
    received_at = datetime.fromisoformat(item["received_at"])
    age_seconds = max(0, int((datetime.now(timezone.utc) - received_at).total_seconds()))
    item["age_seconds"] = age_seconds
    item["connectivity"] = "ONLINE" if age_seconds <= DEVICE_OFFLINE_SECONDS else "OFFLINE"
    if monitoring is not None:
        item["monitoring"] = monitoring
        item["connectivity"] = "ONLINE" if monitoring.get("report_age_seconds", DEVICE_OFFLINE_SECONDS+1) <= DEVICE_OFFLINE_SECONDS else "OFFLINE"
    item["person_status"] = "UNKNOWN"  # A received window score is not a verified diagnosis.
    if item["state"] == "NORMAL":
        item["detection_result"] = "NORMAL"
    elif item["state"] == "INVALID":
        item["detection_result"] = "INVALID"
    else:
        item["detection_result"] = "FALL"
    return item


def record_operation(conn, alert_id, action, operated_at, operated_by, before, after):
    """Append human-field snapshots in the SAME transaction as the alert update."""
    fields = ("status", "acknowledged_at", "acknowledged_by", "alarm_required",
              "alarm_marked_at", "alarm_marked_by", "judgment_category", "judgment_note",
              "note_revision", "note_updated_at", "note_updated_by", "processing_complete", "processed_at", "processed_by")
    snapshots = [json.dumps({key: item[key] for key in fields}, ensure_ascii=False)
                 for item in (to_dict(before), to_dict(after))]
    conn.execute("""INSERT INTO alert_operations
        (alert_id, action, operated_at, operated_by, before_json, after_json)
        VALUES (?, ?, ?, ?, ?, ?)""", (alert_id, action, operated_at, operated_by, *snapshots))


def operation_dict(row):
    item = dict(row)
    item["before"] = json.loads(item.pop("before_json"))
    item["after"] = json.loads(item.pop("after_json"))
    return item


initialise_database()


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    if x_api_key is None or not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


@app.get("/")
def home():
    return {"message": "Fall Detection API is running", "mode": "edge-inference"}


@app.get("/api/v1/health")
def health():
    return {"status": "ok", "inference_location": "Nano edge device"}


@app.get("/dashboard", include_in_schema=False)
def dashboard():
    if not DASHBOARD_PATH.exists():
        raise HTTPException(status_code=500, detail="dashboard.html is missing")
    return FileResponse(DASHBOARD_PATH)


@app.post(
    "/api/v1/telemetry",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
)
def receive_telemetry(data: TelemetryRequest, response: Response):
    """Accept one board-side result; retried messages are idempotent."""
    received_at = now()
    with closing(db_connection()) as conn:
        cursor = conn.execute("""
            INSERT OR IGNORE INTO telemetry_events
            (device_id, session_id, sequence, uptime_ms, model_version, pipeline_version,
             fall_probability, threshold, state, is_test, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (data.device_id, data.session_id, data.sequence, data.uptime_ms,
              data.model_version, data.pipeline_version, data.fall_probability,
              data.threshold, data.state, int(data.is_test), received_at))

        if cursor.rowcount == 0:
            response.status_code = status.HTTP_200_OK
            original = conn.execute(
                """SELECT received_at FROM telemetry_events
                   WHERE device_id=? AND session_id=? AND sequence=?""",
                (data.device_id, data.session_id, data.sequence),
            ).fetchone()
            return {"status": "duplicate", "device_id": data.device_id,
                    "session_id": data.session_id, "sequence": data.sequence,
                    "alert_created": False,
                    "server_received_at": original["received_at"]}

        alert_created = data.state == "NEW_ALARM"
        if data.state == "NEW_ALARM":
            conn.execute("""
                INSERT INTO alerts (telemetry_event_id, device_id, is_test, created_at)
                VALUES (?, ?, ?, ?)
            """, (cursor.lastrowid, data.device_id, int(data.is_test), received_at))
        event = conn.execute("SELECT * FROM telemetry_events WHERE id=?", (cursor.lastrowid,)).fetchone()
        event_evidence.ingest(conn, event)
        incident_id = incident_engine.ingest(conn, event, DEVICE_OFFLINE_SECONDS)
        conn.commit()

    return {"status": "accepted", "device_id": data.device_id,
            "session_id": data.session_id, "sequence": data.sequence,
            "alert_created": alert_created, "incident_id": incident_id,
            "server_received_at": received_at}


@app.get("/api/v1/incidents")
def list_incidents(device_id: str | None = None, hide_tests: bool = False,
                   open_only: bool = False, limit: int = Query(50, ge=1, le=200),
                   before_id: int | None = Query(None, ge=1),
                   workflow_state: Literal["UNREAD", "VERIFY", "COMPLETED", "PENDING"] | None = None):
    conditions, params = [], []
    if device_id:
        conditions.append("i.device_id=?"); params.append(device_id)
    if before_id is not None:
        conditions.append("i.id<?"); params.append(before_id)
    if hide_tests:
        conditions.append("a.is_test=0")
    if open_only:
        conditions.append("EXISTS (SELECT 1 FROM incident_alerts m JOIN alerts x ON x.id=m.alert_id WHERE m.incident_id=i.id AND x.status='OPEN')")
    unread = "EXISTS (SELECT 1 FROM incident_alerts m JOIN alerts x ON x.id=m.alert_id WHERE m.incident_id=i.id AND x.status='OPEN')"
    unfinished = "EXISTS (SELECT 1 FROM incident_alerts m JOIN alerts x ON x.id=m.alert_id WHERE m.incident_id=i.id AND x.processing_complete=0)"
    if workflow_state == "UNREAD":
        conditions.append(unread)
    elif workflow_state == "VERIFY":
        conditions.append(f"NOT ({unread}) AND ({unfinished})")
    elif workflow_state == "COMPLETED":
        conditions.append(f"NOT ({unread}) AND NOT ({unfinished})")
    elif workflow_state == "PENDING":
        conditions.append(f"(({unread}) OR ({unfinished}))")
    query = "SELECT i.id FROM incidents i JOIN alerts a ON a.id=i.first_alert_id"
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY i.id DESC LIMIT ?"; params.append(limit)
    with closing(db_connection()) as conn:
        conn.execute("BEGIN")
        stamp = now()
        rows = conn.execute(query, params).fetchall()
        return [incident_engine.read(conn, row["id"], stamp) for row in rows]


@app.get("/api/v1/incidents/{incident_id}")
def incident_detail(incident_id: int):
    with closing(db_connection()) as conn:
        conn.execute("BEGIN")
        stamp = now()
        result = incident_engine.read(conn, incident_id, stamp)
        if result is None:
            raise HTTPException(status_code=404, detail="Incident not found")
        result["current_monitoring"] = event_evidence.reporting_health(
            conn, result["device_id"], stamp, DEVICE_OFFLINE_SECONDS)
        result["workflow_operations"] = workflow.history(conn, incident_id)
        return result


@app.patch("/api/v1/incidents/{incident_id}/workflow", dependencies=[Depends(require_api_key)])
def update_workflow(incident_id: int, data: workflow.WorkflowRequest):
    with closing(db_connection()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        stamp = now()
        changed = workflow.apply(conn, incident_id, data, stamp, record_operation)
        result = incident_engine.read(conn, incident_id, stamp)
        result["current_monitoring"] = event_evidence.reporting_health(conn, result["device_id"], stamp, DEVICE_OFFLINE_SECONDS)
        result["workflow_operations"] = workflow.history(conn, incident_id)
        conn.commit()
    return {"status": changed, "incident": result}


@app.get("/api/v1/devices/{device_id}/latest")
def latest_device_status(device_id: str):
    with closing(db_connection()) as conn:
        conn.execute("BEGIN")
        row = conn.execute("""
            SELECT e.* FROM reporting_heads h JOIN reporting_sessions s
              ON s.device_id=h.device_id AND s.session_id=h.session_id
            JOIN telemetry_events e ON e.id=s.highwater_event_id WHERE h.device_id=?
        """, (device_id,)).fetchone()
        monitoring = event_evidence.reporting_health(conn, device_id, now(), DEVICE_OFFLINE_SECONDS)
    if row is None:
        raise HTTPException(status_code=404, detail="No telemetry found for this device")
    return device_status(row, monitoring)


@app.get("/api/v1/devices")
def list_devices():
    with closing(db_connection()) as conn:
        conn.execute("BEGIN")
        rows = conn.execute("""
            SELECT e.* FROM reporting_heads h JOIN reporting_sessions s
              ON s.device_id=h.device_id AND s.session_id=h.session_id
            JOIN telemetry_events e ON e.id=s.highwater_event_id
            ORDER BY e.id DESC
        """).fetchall()
        devices = [device_status(row, event_evidence.reporting_health(conn,row["device_id"],now(),DEVICE_OFFLINE_SECONDS)) for row in rows]
    return devices


@app.get("/api/v1/events")
def list_events(device_id: str | None = None, limit: int = Query(50, ge=1, le=200)):
    query, params = "SELECT * FROM telemetry_events", []
    if device_id:
        query += " WHERE device_id=?"
        params.append(device_id)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with closing(db_connection()) as conn:
        rows = conn.execute(query, params).fetchall()
    return [to_dict(row) for row in rows]


@app.get("/api/v1/alerts")
def list_alerts(status: Literal["OPEN", "ACKNOWLEDGED"] | None = None,
                limit: int = Query(50, ge=1, le=200)):
    query = """SELECT alerts.*, telemetry_events.fall_probability, telemetry_events.threshold,
                      telemetry_events.state, telemetry_events.received_at
               FROM alerts JOIN telemetry_events ON telemetry_events.id=alerts.telemetry_event_id"""
    params = []
    if status:
        query += " WHERE alerts.status=?"
        params.append(status)
    query += " ORDER BY alerts.id DESC LIMIT ?"
    params.append(limit)
    with closing(db_connection()) as conn:
        rows = conn.execute(query, params).fetchall()
    return [to_dict(row) for row in rows]


@app.get("/api/v1/alerts/{alert_id}/context")
def alert_context(
    alert_id: int,
    before: int = Query(5, ge=0, le=20),
    after: int = Query(5, ge=0, le=20),
):
    """Return one alert and nearby telemetry from the same device only."""
    with closing(db_connection()) as conn:
        conn.execute("BEGIN")  # One read snapshot for the alert, context and history.
        alert_row = conn.execute(
            "SELECT * FROM alerts WHERE id=?",
            (alert_id,),
        ).fetchone()
        if alert_row is None:
            raise HTTPException(status_code=404, detail="Alert not found")

        trigger_row = conn.execute(
            "SELECT * FROM telemetry_events WHERE id=?",
            (alert_row["telemetry_event_id"],),
        ).fetchone()
        if trigger_row is None:
            raise HTTPException(status_code=500, detail="Alert trigger event is missing")

        device_id = alert_row["device_id"]
        trigger_id = trigger_row["id"]
        before_total = conn.execute(
            "SELECT COUNT(*) FROM telemetry_events WHERE device_id=? AND id<?",
            (device_id, trigger_id),
        ).fetchone()[0]
        after_total = conn.execute(
            "SELECT COUNT(*) FROM telemetry_events WHERE device_id=? AND id>?",
            (device_id, trigger_id),
        ).fetchone()[0]

        before_rows = []
        if before:
            before_rows = conn.execute(
                """SELECT * FROM telemetry_events
                   WHERE device_id=? AND id<? ORDER BY id DESC LIMIT ?""",
                (device_id, trigger_id, before),
            ).fetchall()
            before_rows.reverse()

        after_rows = []
        if after:
            after_rows = conn.execute(
                """SELECT * FROM telemetry_events
                   WHERE device_id=? AND id>? ORDER BY id ASC LIMIT ?""",
                (device_id, trigger_id, after),
            ).fetchall()
        operation_rows = conn.execute(
            "SELECT * FROM alert_operations WHERE alert_id=? ORDER BY id DESC LIMIT 50",
            (alert_id,),
        ).fetchall()
        operation_total = conn.execute(
            "SELECT COUNT(*) FROM alert_operations WHERE alert_id=?", (alert_id,),
        ).fetchone()[0]
        evidence = event_evidence.read_evidence(conn, alert_id)
        membership = conn.execute("SELECT incident_id FROM incident_alerts WHERE alert_id=?", (alert_id,)).fetchone()

    alert = to_dict(alert_row)
    trigger_event = to_dict(trigger_row)
    before_events = [to_dict(row) for row in before_rows]
    after_events = [to_dict(row) for row in after_rows]
    context_events = [
        *[event | {"relation": "before"} for event in before_events],
        trigger_event | {"relation": "trigger"},
        *[event | {"relation": "after"} for event in after_events],
    ]
    return {
        "alert": alert,
        "incident_id": membership["incident_id"] if membership else None,
        "trigger_event": trigger_event,
        "context_events": context_events,
        "operations": [operation_dict(row) for row in operation_rows],
        "operations_total": operation_total,
        "evidence": evidence,
        "context": {
            "requested_before": before,
            "requested_after": after,
            "available_before": before_total,
            "available_after": after_total,
            "returned_before": len(before_events),
            "returned_after": len(after_events),
            "has_more_before": before_total > len(before_events),
            "has_more_after": after_total > len(after_events),
        },
    }


@app.get("/api/v1/alerts/{alert_id}/evidence")
def alert_evidence(alert_id: int):
    with closing(db_connection()) as conn:
        conn.execute("BEGIN")
        row = conn.execute("SELECT device_id FROM alerts WHERE id=?", (alert_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Alert not found")
        evidence = event_evidence.read_evidence(conn, alert_id)
        monitoring = event_evidence.reporting_health(conn,row["device_id"],now(),DEVICE_OFFLINE_SECONDS)
    return {"evidence":evidence, "current_monitoring":monitoring}


def set_review_status(alert_id: int, confirmed: bool, updated_by: str) -> dict:
    """Reviewing a record does not change its manual alarm marker or model result."""
    with closing(db_connection()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        alert = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        if alert is None:
            raise HTTPException(status_code=404, detail="Alert not found")
        target_status = "ACKNOWLEDGED" if confirmed else "OPEN"
        if alert["status"] == target_status:
            return {"status": "unchanged", "alert": to_dict(alert)}
        changed_at = now()
        conn.execute("""
            UPDATE alerts SET status=?, acknowledged_at=?, acknowledged_by=?,
                processing_complete=CASE WHEN ? THEN processing_complete ELSE 0 END,
                processed_at=CASE WHEN ? THEN processed_at ELSE NULL END,
                processed_by=CASE WHEN ? THEN processed_by ELSE NULL END
            WHERE id=?
        """, (target_status, changed_at if confirmed else None,
              updated_by if confirmed else None, confirmed, confirmed, confirmed, alert_id))
        updated = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        record_operation(conn, alert_id, "REVIEW", changed_at, updated_by, alert, updated)
        workflow.legacy_change(conn, alert, updated, "REVIEW", changed_at, updated_by)
        conn.commit()
    return {"status": "updated", "alert": to_dict(updated)}


@app.patch(
    "/api/v1/alerts/{alert_id}/review",
    dependencies=[Depends(require_api_key)],
)
def review_alert(alert_id: int, data: ReviewRequest):
    return set_review_status(alert_id, data.confirmed, data.updated_by)


@app.patch(
    "/api/v1/alerts/{alert_id}/alarm-mark",
    dependencies=[Depends(require_api_key)],
)
def mark_alarm(alert_id: int, data: AlarmMarkRequest):
    """Store a human's alarm marker; this endpoint does not send notifications."""
    with closing(db_connection()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        alert = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        if alert is None:
            raise HTTPException(status_code=404, detail="Alert not found")
        if bool(alert["alarm_required"]) == data.alarm_required:
            return {"status": "unchanged", "alert": to_dict(alert)}
        changed_at = now()
        conn.execute("""
            UPDATE alerts SET alarm_required=?, alarm_marked_at=?, alarm_marked_by=?,
                processing_complete=CASE WHEN ? THEN 0 ELSE processing_complete END,
                processed_at=CASE WHEN ? THEN NULL ELSE processed_at END,
                processed_by=CASE WHEN ? THEN NULL ELSE processed_by END
            WHERE id=?
        """, (int(data.alarm_required), changed_at, data.updated_by, data.alarm_required, data.alarm_required, data.alarm_required, alert_id))
        updated = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        record_operation(conn, alert_id, "ALARM_MARK", changed_at, data.updated_by, alert, updated)
        workflow.legacy_change(conn, alert, updated, "ALARM_MARK", changed_at, data.updated_by, clear_handling=data.alarm_required)
        conn.commit()
    return {"status": "updated", "alert": to_dict(updated)}


@app.patch("/api/v1/alerts/{alert_id}/note", dependencies=[Depends(require_api_key)])
def update_note(alert_id: int, data: NoteRequest):
    with closing(db_connection()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        alert = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        if alert is None:
            raise HTTPException(status_code=404, detail="Alert not found")
        if data.category == alert["judgment_category"] and data.note == alert["judgment_note"]:
            return {"status": "unchanged", "alert": to_dict(alert)}
        if data.expected_revision != alert["note_revision"]:
            raise HTTPException(status_code=409, detail="Note changed; reload before saving")
        changed_at = now()
        conn.execute("""UPDATE alerts SET judgment_category=?, judgment_note=?,
            note_revision=note_revision+1, note_updated_at=?, note_updated_by=?,
            processing_complete=0, processed_at=NULL, processed_by=NULL WHERE id=?""",
            (data.category, data.note, changed_at, data.updated_by, alert_id))
        updated = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        record_operation(conn, alert_id, "NOTE", changed_at, data.updated_by, alert, updated)
        workflow.legacy_change(conn, alert, updated, "NOTE", changed_at, data.updated_by)
        conn.commit()
    return {"status": "updated", "alert": to_dict(updated)}


@app.get("/api/v1/alerts/{alert_id}/operations")
def operation_history(alert_id: int, limit: int = Query(50, ge=1, le=200),
                      before_id: int | None = Query(None, ge=1)):
    with closing(db_connection()) as conn:
        if conn.execute("SELECT 1 FROM alerts WHERE id=?", (alert_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Alert not found")
        query = "SELECT * FROM alert_operations WHERE alert_id=?"
        params = [alert_id]
        if before_id is not None:
            query += " AND id<?"
            params.append(before_id)
        rows = conn.execute(query + " ORDER BY id DESC LIMIT ?", [*params, limit + 1]).fetchall()
    has_more = len(rows) > limit
    items = [operation_dict(row) for row in rows[:limit]]
    return {"items": items, "has_more": has_more,
            "next_before_id": items[-1]["id"] if has_more else None}


@app.patch(
    "/api/v1/alerts/{alert_id}/acknowledge",
    dependencies=[Depends(require_api_key)],
)
def acknowledge_alert(alert_id: int, data: AcknowledgeRequest):
    result = set_review_status(alert_id, True, data.acknowledged_by)
    result_status = "already_acknowledged" if result["status"] == "unchanged" else "acknowledged"
    return {"status": result_status, "alert_id": alert_id}
