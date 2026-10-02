"""Conservative event evidence and reporting health; no fall classification rules."""
from datetime import datetime, timezone
import json

POLICY_VERSION = "received_telemetry_evidence_v1"
BEFORE_MS = AFTER_MS = 10000
MAX_POINTS = 200
GAP_MS = 7500  # Engineering warning for a nominal 5 s status upload, not an IMU rule.


def utc(value):
    value = datetime.fromisoformat(value)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def initialise(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS event_evidence (
        alert_id INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
        policy_version TEXT NOT NULL, revision INTEGER NOT NULL,
        updated_at TEXT NOT NULL, source TEXT NOT NULL, evidence_json TEXT NOT NULL,
        FOREIGN KEY(alert_id) REFERENCES alerts(id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS reporting_sessions (
        device_id TEXT NOT NULL, session_id TEXT NOT NULL,
        baseline_at TEXT NOT NULL, baseline_uptime_ms INTEGER NOT NULL,
        last_received_at TEXT NOT NULL, last_progress_at TEXT NOT NULL,
        highwater_uptime_ms INTEGER NOT NULL, highwater_sequence INTEGER NOT NULL,
        highwater_event_id INTEGER NOT NULL, received_count INTEGER NOT NULL, progress_count INTEGER NOT NULL,
        PRIMARY KEY(device_id, session_id),
        FOREIGN KEY(highwater_event_id) REFERENCES telemetry_events(id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS reporting_heads (
        device_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
        session_changed_at TEXT, session_count INTEGER NOT NULL,
        FOREIGN KEY(device_id, session_id) REFERENCES reporting_sessions(device_id, session_id))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_session_uptime ON telemetry_events(device_id, session_id, uptime_ms, sequence)")
    conn.execute("CREATE TABLE IF NOT EXISTS evidence_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
    if conn.execute("SELECT 1 FROM evidence_meta WHERE name='initialised'").fetchone():
        return
    # Reconstruct only already-stored evidence, not historical health incidents or diagnoses.
    for event in conn.execute("SELECT * FROM telemetry_events ORDER BY id"):
        observe_report(conn, event)
    for alert in conn.execute("SELECT id FROM alerts ORDER BY id"):
        save_evidence(conn, alert["id"], "reconstructed", None)
    conn.execute("INSERT INTO evidence_meta VALUES ('initialised', ?)", (POLICY_VERSION,))


def observe_report(conn, event):
    device, session, stamp = event["device_id"], event["session_id"], event["received_at"]
    old = conn.execute("SELECT * FROM reporting_sessions WHERE device_id=? AND session_id=?",
                       (device, session)).fetchone()
    if old is None:
        conn.execute("""INSERT INTO reporting_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0)""",
                     (device, session, stamp, event["uptime_ms"], stamp, stamp,
                      event["uptime_ms"], event["sequence"], event["id"]))
        head = conn.execute("SELECT * FROM reporting_heads WHERE device_id=?", (device,)).fetchone()
        if head is None:
            conn.execute("INSERT INTO reporting_heads VALUES (?, ?, NULL, 1)", (device, session))
        else:
            conn.execute("UPDATE reporting_heads SET session_id=?, session_changed_at=?, session_count=session_count+1 WHERE device_id=?",
                         (session, stamp, device))
        return
    # A known older session cannot switch the device back when its queued messages arrive.
    newer = (event["uptime_ms"] >= old["highwater_uptime_ms"]
             and event["sequence"] > old["highwater_sequence"])
    advances = newer and event["uptime_ms"] > old["highwater_uptime_ms"]
    if newer:
        conn.execute("""UPDATE reporting_sessions SET last_received_at=?, last_progress_at=?,
            highwater_uptime_ms=?, highwater_sequence=?, highwater_event_id=?, received_count=received_count+1,
            progress_count=progress_count+? WHERE device_id=? AND session_id=?""",
            (stamp, stamp if advances else old["last_progress_at"], event["uptime_ms"],
             event["sequence"], event["id"], int(advances), device, session))
    else:
        conn.execute("""UPDATE reporting_sessions SET last_received_at=?, received_count=received_count+1
            WHERE device_id=? AND session_id=?""", (stamp, device, session))


def reporting_health(conn, device, reference_time, offline_seconds):
    head = conn.execute("""SELECT s.*, h.session_changed_at, h.session_count, e.state
        FROM reporting_heads h JOIN reporting_sessions s
          ON s.device_id=h.device_id AND s.session_id=h.session_id
        JOIN telemetry_events e ON e.id=s.highwater_event_id WHERE h.device_id=?""", (device,)).fetchone()
    if head is None:
        return {"status":"UNKNOWN", "reasons":["NO_RECEIVED_TELEMETRY"], "sampling_health":"UNVERIFIED"}
    reference = utc(reference_time)
    age = max(0, int((reference - utc(head["last_received_at"])).total_seconds()))
    progress_age = max(0, int((reference - utc(head["last_progress_at"])).total_seconds()))
    growth = max(0, int((utc(head["last_progress_at"]) - utc(head["baseline_at"])).total_seconds()*1000)
                 - (head["highwater_uptime_ms"] - head["baseline_uptime_ms"]))
    reasons = []
    if age > offline_seconds:
        state = "UNAVAILABLE"; reasons.append("NO_RECENT_REPORT")
    elif head["state"] == "INVALID":
        state = "INVALID"; reasons.append("BOARD_REPORTED_INVALID")
    elif progress_age > offline_seconds:
        state = "PROGRESS_UNVERIFIED"; reasons.append("NO_RECENT_DEVICE_TIME_ADVANCE")
    elif growth > offline_seconds * 1000:
        state = "DELAY_SUSPECTED"; reasons.append("RECEIPT_TIME_OUTPACED_DEVICE_TIME")
    elif head["progress_count"] == 0:
        state = "WARMING_UP"; reasons.append("DEVICE_TIME_ADVANCE_NOT_YET_OBSERVED")
    else:
        state = "REPORTING"; reasons.append("RECENT_REPORT_AND_DEVICE_TIME_ADVANCE")
    if head["session_changed_at"]:
        reasons.append("SESSION_CHANGE_OBSERVED")
    return {"status":state, "reasons":reasons, "sampling_health":"UNVERIFIED",
            "active_session_id":head["session_id"], "session_count":head["session_count"],
            "last_session_change_at":head["session_changed_at"], "last_report_at":head["last_received_at"],
            "last_progress_at":head["last_progress_at"], "report_age_seconds":age,
            "progress_age_seconds":progress_age, "relative_lag_growth_ms":growth,
            "offline_after_seconds":offline_seconds,
            "limitation":"Cloud reporting health only; no raw IMU or absolute capture clock. Does not establish personal safety or detect the cause of disconnection."}


def build_evidence(conn, alert_id):
    trigger = conn.execute("""SELECT e.* FROM alerts a JOIN telemetry_events e ON e.id=a.telemetry_event_id
        WHERE a.id=?""", (alert_id,)).fetchone()
    if trigger is None:
        raise ValueError("Alert trigger not found")
    start, end = max(0, trigger["uptime_ms"]-BEFORE_MS), trigger["uptime_ms"]+AFTER_MS
    args = (trigger["device_id"], trigger["session_id"], start, end)
    total = conn.execute("""SELECT COUNT(*) FROM telemetry_events
        WHERE device_id=? AND session_id=? AND uptime_ms BETWEEN ? AND ?""", args).fetchone()[0]
    # Always retain the trigger even when the time span contains many reports.
    rows = conn.execute("""SELECT * FROM telemetry_events WHERE device_id=? AND session_id=?
        AND uptime_ms BETWEEN ? AND ? AND id!=?
        ORDER BY ABS(uptime_ms-?), uptime_ms, sequence, id LIMIT ?""",
        (*args, trigger["id"], trigger["uptime_ms"], MAX_POINTS-1)).fetchall()
    rows = sorted([trigger, *rows], key=lambda row:(row["uptime_ms"],row["sequence"],row["id"]))
    same_configuration = lambda row: (row["model_version"], row["pipeline_version"], row["threshold"],row["is_test"]) == (
        trigger["model_version"],trigger["pipeline_version"],trigger["threshold"],trigger["is_test"])
    valid = [row for row in rows if same_configuration(row) and row["state"] != "INVALID"]
    positives = [row for row in valid if row["state"] in {"NEW_ALARM","POSITIVE"}]
    after = [row for row in valid if row["uptime_ms"] > trigger["uptime_ms"]]
    gaps = [(right["uptime_ms"]-left["uptime_ms"]) for left,right in zip(rows,rows[1:])]
    jumps = [(right["sequence"]-left["sequence"]-1) for left,right in zip(rows,rows[1:])]
    warnings = ["RAW_IMU_NOT_AVAILABLE", "NOT_ALL_INFERENCE_WINDOWS_UPLOADED"]
    if not any(row["uptime_ms"] < trigger["uptime_ms"] for row in rows): warnings.append("NO_PRE_TRIGGER_REPORT")
    if not after: warnings.append("NO_POST_TRIGGER_VALID_REPORT")
    if any(row["state"] == "INVALID" for row in rows): warnings.append("INVALID_REPORT_IN_SPAN")
    if any(not same_configuration(row) for row in rows): warnings.append("CONFIGURATION_CHANGED_IN_SPAN")
    if any(gap > GAP_MS for gap in gaps): warnings.append("RECEIVED_REPORT_GAP")
    if total > MAX_POINTS: warnings.append("EVIDENCE_POINTS_TRUNCATED")
    if any(jump < 0 for jump in jumps): warnings.append("DEVICE_ORDER_INCONSISTENT")
    incoming = sorted(rows,key=lambda row:row["id"])
    if [row["id"] for row in incoming] != [row["id"] for row in rows]: warnings.append("OUT_OF_ORDER_RECEIPT")
    if trigger["uptime_ms"] < BEFORE_MS: warnings.append("EARLY_SESSION_TRIGGER")
    max_seen = conn.execute("SELECT MAX(uptime_ms) FROM telemetry_events WHERE device_id=? AND session_id=?",
                           args[:2]).fetchone()[0]
    reached = max_seen >= end
    peak = max(row["fall_probability"] for row in positives)
    associated = conn.execute("""SELECT a.id FROM alerts a JOIN telemetry_events e ON e.id=a.telemetry_event_id
        WHERE e.device_id=? AND e.session_id=? AND e.uptime_ms BETWEEN ? AND ? ORDER BY e.uptime_ms,e.sequence""", args).fetchall()
    points = [{"event_id":row["id"], "uptime_ms":row["uptime_ms"], "sequence":row["sequence"],
               "state":row["state"], "score":row["fall_probability"], "threshold":row["threshold"],
               "received_at":row["received_at"], "same_configuration":same_configuration(row),
               "is_trigger":row["id"] == trigger["id"]} for row in rows]
    return {"event_key":f"fall-event-{alert_id}", "policy_version":POLICY_VERSION,
        "device_id":trigger["device_id"], "session_id":trigger["session_id"],
        "trigger_event_id":trigger["id"], "trigger_uptime_ms":trigger["uptime_ms"],
        "trigger_score":trigger["fall_probability"], "threshold":trigger["threshold"],
        "model_version":trigger["model_version"], "pipeline_version":trigger["pipeline_version"],
        "span_start_ms":start, "span_end_ms":end, "post_span_reached":reached,
        "received_point_count":total, "returned_point_count":len(points),
        "positive_report_count":len(positives), "peak_received_score":peak,
        "positive_observed_span_ms":max(row["uptime_ms"] for row in positives)-min(row["uptime_ms"] for row in positives),
        "first_post_trigger_state":after[0]["state"] if after else None,
        "max_received_report_gap_ms":max(gaps,default=0),
        "omitted_sequence_count":sum(max(0,jump) for jump in jumps),
        "related_alert_ids":[row["id"] for row in associated],
        "warnings":warnings, "points":points,
        "assessment":"CANDIDATE_ONLY", "raw_imu_available":False,
        "limitation":"Received window scores only. No proof of impact, posture, inactivity, a continuous positive duration, a real fall or personal safety. Sequence gaps can reflect deliberate coalescing, not just loss."}


def save_evidence(conn, alert_id, source, updated_at):
    evidence = build_evidence(conn, alert_id)
    payload = json.dumps(evidence,ensure_ascii=False,sort_keys=True)
    old = conn.execute("SELECT * FROM event_evidence WHERE alert_id=?", (alert_id,)).fetchone()
    if old is not None and old["evidence_json"] == payload:
        return
    updated_at = updated_at or datetime.now(timezone.utc).isoformat()
    conn.execute("""INSERT INTO event_evidence VALUES (?, ?, ?, 1, ?, ?, ?)
        ON CONFLICT(alert_id) DO UPDATE SET revision=revision+1, updated_at=excluded.updated_at,
        policy_version=excluded.policy_version, source=excluded.source, evidence_json=excluded.evidence_json""",
        (alert_id,evidence["event_key"],POLICY_VERSION,updated_at,source,payload))


def ingest(conn, event):
    observe_report(conn,event)
    ids = conn.execute("""SELECT a.id FROM alerts a JOIN telemetry_events e ON e.id=a.telemetry_event_id
        LEFT JOIN event_evidence v ON v.alert_id=a.id
        WHERE e.device_id=? AND e.session_id=? AND e.uptime_ms<=?
          AND (e.uptime_ms>=? OR json_extract(v.evidence_json,'$.post_span_reached')=0)""",
        (event["device_id"],event["session_id"],event["uptime_ms"]+BEFORE_MS,
         max(0,event["uptime_ms"]-AFTER_MS))).fetchall()
    for row in ids:
        save_evidence(conn,row["id"],"live_ingest",event["received_at"])


def read_evidence(conn, alert_id):
    row = conn.execute("SELECT * FROM event_evidence WHERE alert_id=?", (alert_id,)).fetchone()
    if row is None: return None
    payload = json.loads(row["evidence_json"])
    payload.update(revision=row["revision"],updated_at=row["updated_at"],source=row["source"])
    return payload
