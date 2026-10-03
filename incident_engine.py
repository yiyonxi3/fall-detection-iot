"""Bounded candidate association and received-evidence quality, not a fall classifier."""
from datetime import datetime, timezone
import json

POLICY_VERSION = "bounded_incidents_v1"
ASSOCIATION_MS = 10000
POST_MS = 10000
REPORT_GAP_MS = 7500


def utc(value):
    stamp = datetime.fromisoformat(value)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def configuration(row):
    return tuple(row[key] for key in ("model_version", "pipeline_version", "threshold", "is_test"))


def initialise(conn, offline_seconds):
    conn.execute("""CREATE TABLE IF NOT EXISTS incidents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        first_alert_id INTEGER NOT NULL UNIQUE REFERENCES alerts(id),
        device_id TEXT NOT NULL, session_id TEXT NOT NULL,
        policy_version TEXT NOT NULL, source TEXT NOT NULL,
        created_at TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}',
        revision INTEGER NOT NULL DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS incident_alerts (
        alert_id INTEGER PRIMARY KEY REFERENCES alerts(id),
        incident_id INTEGER NOT NULL REFERENCES incidents(id),
        relation_reason TEXT NOT NULL, associated_at TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_incident_members ON incident_alerts(incident_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_incident_session ON incidents(device_id, session_id, id)")
    conn.execute("CREATE TABLE IF NOT EXISTS incident_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
    if conn.execute("SELECT 1 FROM incident_meta WHERE name='initialised'").fetchone():
        return
    # Arrival-order reconstruction gives stable membership without changing human fields.
    for row in conn.execute("""SELECT a.id AS alert_id, e.* FROM alerts a
        JOIN telemetry_events e ON e.id=a.telemetry_event_id ORDER BY e.id""").fetchall():
        associate(conn, row, "reconstructed", offline_seconds)
    for row in conn.execute("SELECT id FROM incidents").fetchall():
        save_evidence(conn, row["id"], offline_seconds)
    conn.execute("INSERT INTO incident_meta VALUES ('initialised', ?)", (POLICY_VERSION,))


def associate(conn, event, source, offline_seconds):
    alert_id = event["alert_id"]
    existing = conn.execute("SELECT incident_id FROM incident_alerts WHERE alert_id=?", (alert_id,)).fetchone()
    if existing:
        return existing["incident_id"]
    anchors = conn.execute("""SELECT i.id AS incident_id, e.* FROM incidents i
        JOIN alerts a ON a.id=i.first_alert_id JOIN telemetry_events e ON e.id=a.telemetry_event_id
        JOIN incident_alerts first_member ON first_member.alert_id=i.first_alert_id
        WHERE i.device_id=? AND i.session_id=? AND first_member.relation_reason!='LATE_OUT_OF_ORDER_CANDIDATE'
        ORDER BY i.id DESC""",
        (event["device_id"], event["session_id"])).fetchall()
    reason = "FIRST_CANDIDATE"
    previous_newer = conn.execute("""SELECT 1 FROM telemetry_events
        WHERE device_id=? AND session_id=? AND id<? AND (uptime_ms>? OR sequence>?) LIMIT 1""",
        (event["device_id"], event["session_id"], event["id"], event["uptime_ms"], event["sequence"])).fetchone()
    if previous_newer:
        anchors = []
        reason = "LATE_OUT_OF_ORDER_CANDIDATE"
    for anchor in anchors:
        delta = event["uptime_ms"] - anchor["uptime_ms"]
        if not 0 <= delta <= ASSOCIATION_MS or event["sequence"] < anchor["sequence"]:
            reason = "OUTSIDE_ANCHOR_WINDOW_OR_LATE"; continue
        if configuration(event) != configuration(anchor):
            reason = "CONFIGURATION_BOUNDARY"; continue
        # Never bridge a reboot/configuration change or a long arrival interruption.
        intervening = conn.execute("""SELECT * FROM telemetry_events WHERE device_id=?
            AND id>? AND id<=? ORDER BY id""", (event["device_id"], anchor["id"], event["id"])).fetchall()
        if any(row["session_id"] != event["session_id"] for row in intervening):
            reason = "SESSION_BOUNDARY"; continue
        if any(configuration(row) != configuration(anchor) for row in intervening):
            reason = "CONFIGURATION_BOUNDARY"; continue
        if (utc(event["received_at"]) - utc(anchor["received_at"])).total_seconds() > offline_seconds:
            reason = "RECEIPT_WINDOW_EXPIRED"; continue
        incident_id = anchor["incident_id"]
        reason = "SAME_SESSION_CONFIG_WITHIN_ANCHOR_10S"
        break
    else:
        cursor = conn.execute("""INSERT INTO incidents
            (first_alert_id, device_id, session_id, policy_version, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""", (alert_id, event["device_id"], event["session_id"],
                POLICY_VERSION, source, event["received_at"]))
        incident_id = cursor.lastrowid
    conn.execute("INSERT INTO incident_alerts VALUES (?, ?, ?, ?)",
                 (alert_id, incident_id, reason, event["received_at"]))
    return incident_id


def members(conn, incident_id):
    return conn.execute("""SELECT a.*, e.session_id, e.sequence, e.uptime_ms,
        e.fall_probability, e.threshold, e.state, e.model_version, e.pipeline_version,
        e.received_at, m.relation_reason FROM incident_alerts m
        JOIN alerts a ON a.id=m.alert_id JOIN telemetry_events e ON e.id=a.telemetry_event_id
        WHERE m.incident_id=? ORDER BY e.uptime_ms,e.sequence,a.id""", (incident_id,)).fetchall()


def build_evidence(conn, incident_id, offline_seconds):
    incident = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
    anchor = conn.execute("""SELECT e.* FROM alerts a JOIN telemetry_events e
        ON e.id=a.telemetry_event_id WHERE a.id=?""", (incident["first_alert_id"],)).fetchone()
    candidates = members(conn, incident_id)
    last = candidates[-1]
    horizon = last["uptime_ms"] + POST_MS
    reasons = []
    reached = False
    closed = False
    last_uptime, last_sequence = anchor["uptime_ms"], anchor["sequence"]
    last_progress_at = anchor["received_at"]
    first_post = None
    points = []
    # Evaluate arrival history only until the observation horizon is reached.
    # Later device outages cannot rewrite a completed historical event.
    for row in conn.execute("SELECT * FROM telemetry_events WHERE device_id=? AND id>? ORDER BY id",
                            (incident["device_id"], anchor["id"])):
        if (utc(row["received_at"]) - utc(last_progress_at)).total_seconds() > offline_seconds:
            reasons.append("POST_REPORT_TIMEOUT")
            closed = True
            break
        if row["session_id"] != incident["session_id"]:
            reasons.append("SESSION_CHANGED_BEFORE_HORIZON")
            closed = True
            break
        if configuration(row) != configuration(anchor):
            reasons.append("CONFIGURATION_CHANGED")
            continue
        if row["state"] == "INVALID":
            reasons.append("INVALID_REPORT")
            continue
        if row["sequence"] <= last_sequence or row["uptime_ms"] <= last_uptime:
            continue  # Receipt without valid board-time advance does not close evidence.
        relative_lag = ((utc(row["received_at"]) - utc(anchor["received_at"])).total_seconds()*1000
                        - (row["uptime_ms"] - anchor["uptime_ms"]))
        if relative_lag > offline_seconds*1000:
            reasons.append("RELATIVE_DELAY_SUSPECTED")
        if row["uptime_ms"] - last_uptime > REPORT_GAP_MS:
            reasons.append("RECEIVED_REPORT_GAP")
        last_uptime, last_sequence = row["uptime_ms"], row["sequence"]
        last_progress_at = row["received_at"]
        if row["uptime_ms"] > last["uptime_ms"]:
            first_post = first_post or {"state": row["state"], "score": row["fall_probability"],
                                       "uptime_ms": row["uptime_ms"], "received_at": row["received_at"]}
        if len(points) < 200:
            points.append({"state": row["state"], "score": row["fall_probability"],
                           "uptime_ms": row["uptime_ms"], "sequence": row["sequence"]})
        if row["uptime_ms"] >= horizon and row["sequence"] > last["sequence"]:
            reached = True
            closed = True
            break
    reasons = list(dict.fromkeys(reasons))
    return {"state": "INCOMPLETE" if reasons else "OBSERVED" if reached else "COLLECTING",
            "reasons": reasons, "post_horizon_ms": horizon, "post_horizon_reached": reached,
            "observation_closed": closed,
            "last_valid_progress_at": last_progress_at, "first_post_candidate_report": first_post,
            "received_progress_points": points, "raw_imu_available": False,
            "person_status": "UNKNOWN", "timeout_seconds": offline_seconds}


def save_evidence(conn, incident_id, offline_seconds):
    payload = json.dumps(build_evidence(conn, incident_id, offline_seconds), ensure_ascii=False, sort_keys=True)
    conn.execute("""UPDATE incidents SET evidence_json=?, revision=revision+1
        WHERE id=? AND evidence_json!=?""", (payload, incident_id, payload))


def ingest(conn, event, offline_seconds):
    linked = None
    if event["state"] == "NEW_ALARM":
        alert = conn.execute("SELECT id FROM alerts WHERE telemetry_event_id=?", (event["id"],)).fetchone()
        linked = associate(conn, dict(event) | {"alert_id": alert["id"]}, "live_ingest", offline_seconds)
    # Include unfinished older sessions so a new session exposes interrupted evidence.
    rows = conn.execute("""SELECT id FROM incidents WHERE device_id=? AND
        (json_extract(evidence_json,'$.observation_closed')=0 OR evidence_json='{}' OR id=?)""",
        (event["device_id"], linked)).fetchall()
    for row in rows:
        save_evidence(conn, row["id"], offline_seconds)
    return linked


def read(conn, incident_id, reference_time):
    row = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    evidence = json.loads(result.pop("evidence_json"))
    # Time-only deterioration is computed on public GET; never write on a read.
    if not evidence["observation_closed"] and (utc(reference_time) - utc(evidence["last_valid_progress_at"])).total_seconds() > evidence["timeout_seconds"]:
        evidence["state"] = "INCOMPLETE"
        evidence["reasons"] = list(dict.fromkeys([*evidence["reasons"], "POST_REPORT_TIMEOUT"]))
    candidates = [dict(item) for item in members(conn, incident_id)]
    for item in candidates:
        item["is_test"] = bool(item["is_test"])
        item["alarm_required"] = bool(item["alarm_required"])
        item["processing_complete"] = bool(item["processing_complete"])
    completed = sum(item["processing_complete"] for item in candidates)
    read_all = all(item["status"] == "ACKNOWLEDGED" for item in candidates)
    result.update(incident_key=f"incident-{incident_id}", candidate_count=len(candidates),
        model_state="MULTIPLE_CANDIDATES" if len(candidates) > 1 else "SINGLE_CANDIDATE",
        peak_score=max(item["fall_probability"] for item in candidates),
        first_uptime_ms=candidates[0]["uptime_ms"], last_uptime_ms=candidates[-1]["uptime_ms"],
        last_candidate_at=candidates[-1]["received_at"], is_test=candidates[0]["is_test"],
        reviewed_count=sum(item["status"] == "ACKNOWLEDGED" for item in candidates),
        alarm_marked_count=sum(item["alarm_required"] for item in candidates),
        review_state="REVIEWED" if all(item["status"] == "ACKNOWLEDGED" for item in candidates) else "PENDING",
        completed_count=completed,
        workflow_state="UNREAD" if not read_all else "COMPLETED" if completed == len(candidates) else "VERIFY",
        evidence=evidence, requires_evidence_check=evidence["state"] == "INCOMPLETE",
        association_window_ms=ASSOCIATION_MS, candidates=candidates,
        assessment="SUSPECTED_EVENT_ONLY", evaluated_at=reference_time,
        limitation="Temporal association is not proof of one action or a true fall. Observed horizon covers received reports only; lower scores do not establish safety.")
    return result
