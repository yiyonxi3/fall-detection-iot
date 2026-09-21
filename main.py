"""Backend for Nano-side fall detection: receive, store, and query telemetry."""

from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import secrets
import sqlite3
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

DEFAULT_DB_PATH = Path(__file__).with_name("fall_detection.db")
DB_PATH = Path(os.getenv("FALL_DB_PATH", str(DEFAULT_DB_PATH)))
DASHBOARD_PATH = Path(__file__).with_name("dashboard.html")
API_KEY = os.getenv("FALL_API_KEY", "local-test-key")
DEVICE_OFFLINE_SECONDS = int(os.getenv("DEVICE_OFFLINE_SECONDS", "30"))
app = FastAPI(
    title="IoT Fall Detection API",
    description="Nano performs inference; this server receives, stores and displays results.",
    version="0.4.0",
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
    acknowledged_by: str = Field(min_length=1, max_length=100)


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
            FOREIGN KEY(telemetry_event_id) REFERENCES telemetry_events(id)
        );
        CREATE INDEX IF NOT EXISTS idx_events_device_received
            ON telemetry_events(device_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_alerts_status
            ON alerts(status, id DESC);
        """)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()


def to_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    if "is_test" in item:
        item["is_test"] = bool(item["is_test"])
    return item


def device_status(row: sqlite3.Row) -> dict:
    """Separate connection freshness from the model's last detection result."""
    item = to_dict(row)
    received_at = datetime.fromisoformat(item["received_at"])
    age_seconds = max(0, int((datetime.now(timezone.utc) - received_at).total_seconds()))
    item["age_seconds"] = age_seconds
    item["connectivity"] = "ONLINE" if age_seconds <= DEVICE_OFFLINE_SECONDS else "OFFLINE"
    if item["state"] == "NORMAL":
        item["detection_result"] = "NORMAL"
    elif item["state"] == "INVALID":
        item["detection_result"] = "INVALID"
    else:
        item["detection_result"] = "FALL"
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
        conn.commit()

    return {"status": "accepted", "device_id": data.device_id,
            "session_id": data.session_id, "sequence": data.sequence,
            "alert_created": alert_created, "server_received_at": received_at}


@app.get("/api/v1/devices/{device_id}/latest")
def latest_device_status(device_id: str):
    with closing(db_connection()) as conn:
        row = conn.execute("""
            SELECT * FROM telemetry_events WHERE device_id=? ORDER BY id DESC LIMIT 1
        """, (device_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No telemetry found for this device")
    return device_status(row)


@app.get("/api/v1/devices")
def list_devices():
    with closing(db_connection()) as conn:
        rows = conn.execute("""
            SELECT e.* FROM telemetry_events e
            JOIN (
                SELECT device_id, MAX(id) AS latest_id
                FROM telemetry_events GROUP BY device_id
            ) latest ON e.id=latest.latest_id
            ORDER BY e.id DESC
        """).fetchall()
    return [device_status(row) for row in rows]


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


@app.patch(
    "/api/v1/alerts/{alert_id}/acknowledge",
    dependencies=[Depends(require_api_key)],
)
def acknowledge_alert(alert_id: int, data: AcknowledgeRequest):
    with closing(db_connection()) as conn:
        alert = conn.execute("SELECT status FROM alerts WHERE id=?", (alert_id,)).fetchone()
        if alert is None:
            raise HTTPException(status_code=404, detail="Alert not found")
        if alert["status"] == "ACKNOWLEDGED":
            return {"status": "already_acknowledged", "alert_id": alert_id}
        conn.execute("""
            UPDATE alerts SET status='ACKNOWLEDGED', acknowledged_at=?, acknowledged_by=?
            WHERE id=?
        """, (now(), data.acknowledged_by, alert_id))
        conn.commit()
    return {"status": "acknowledged", "alert_id": alert_id}
