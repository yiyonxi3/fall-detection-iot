"""Explicit human processing workflow; never a fall diagnosis or notification service."""
import json
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, StringConstraints, model_validator

Category = Literal["UNASSESSED", "SIMULATED_TEST", "SUSPECTED_FALSE_POSITIVE", "NEEDS_VERIFICATION", "OTHER"]
PositiveId = Annotated[int, Field(ge=1, strict=True)]
HUMAN_FIELDS = ("status", "acknowledged_at", "acknowledged_by", "alarm_required",
                "alarm_marked_at", "alarm_marked_by", "judgment_category", "judgment_note",
                "note_revision", "note_updated_at", "note_updated_by", "processing_complete",
                "processed_at", "processed_by")


class WorkflowRequest(BaseModel):
    operation: Literal["REVIEW", "UNREVIEW", "SAVE", "FINISH", "REOPEN", "ALARM_MARK", "ALARM_CLEAR"]
    alert_ids: list[PositiveId] = Field(min_length=1, max_length=200)
    expected_member_ids: list[PositiveId] = Field(min_length=1, max_length=10000)
    expected_revision: int = Field(ge=0, strict=True)
    updated_by: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    category: Category | None = None
    note: str | None = Field(default=None, max_length=1000)
    handling_note: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def check_fields(self):
        if len(set(self.alert_ids)) != len(self.alert_ids) or len(set(self.expected_member_ids)) != len(self.expected_member_ids):
            raise ValueError("Duplicate record ids")
        if self.operation in {"SAVE", "FINISH"}:
            if self.category is None or self.note is None:
                raise ValueError("Saving requires a category and note")
        elif self.category is not None or self.note is not None or self.handling_note is not None:
            raise ValueError("These fields are only accepted when saving")
        return self


def initialise(conn):
    # Old viewed or annotated records remain UNCOMPLETED. Never invent past handling.
    for table, additions in {
        "alerts": {"processing_complete": "INTEGER NOT NULL DEFAULT 0 CHECK(processing_complete IN (0, 1))",
                   "processed_at": "TEXT", "processed_by": "TEXT"},
        "incidents": {"workflow_revision": "INTEGER NOT NULL DEFAULT 0",
                      "handling_note": "TEXT NOT NULL DEFAULT ''"},
    }.items():
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    conn.execute("""CREATE TABLE IF NOT EXISTS incident_workflow_operations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        incident_id INTEGER NOT NULL REFERENCES incidents(id),
        action TEXT NOT NULL, operated_at TEXT NOT NULL, operated_by TEXT NOT NULL,
        selected_alert_ids_json TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_workflow_operations ON incident_workflow_operations(incident_id,id DESC)")


def snapshot(incident, records):
    return {"workflow_revision": incident["workflow_revision"], "handling_note": incident["handling_note"],
            "records": [{"id": row["id"], **{key: row[key] for key in HUMAN_FIELDS}} for row in records]}


def append_history(conn, incident_id, action, stamp, operator, before, after):
    conn.execute("""INSERT INTO incident_workflow_operations
        (incident_id,action,operated_at,operated_by,selected_alert_ids_json,before_json,after_json)
        VALUES (?,?,?,?,?,?,?)""", (incident_id, action, stamp, operator,
        json.dumps([row["id"] for row in before["records"]]),
        json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False)))


def history(conn, incident_id, limit=50):
    items = []
    for row in conn.execute("SELECT * FROM incident_workflow_operations WHERE incident_id=? ORDER BY id DESC LIMIT ?",
                            (incident_id, limit)):
        item = dict(row)
        item["selected_alert_ids"] = json.loads(item.pop("selected_alert_ids_json"))
        item["before"] = json.loads(item.pop("before_json"))
        item["after"] = json.loads(item.pop("after_json"))
        items.append(item)
    total = conn.execute("SELECT COUNT(*) FROM incident_workflow_operations WHERE incident_id=?", (incident_id,)).fetchone()[0]
    return {"items": items, "total": total}


def legacy_change(conn, before_alert, after_alert, action, stamp, operator, clear_handling=False):
    """Legacy writes participate in the same version and history, in their existing transaction."""
    member = conn.execute("SELECT incident_id FROM incident_alerts WHERE alert_id=?", (before_alert["id"],)).fetchone()
    if member is None:
        return
    incident_id = member["incident_id"]
    incident = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
    before = snapshot(incident, [before_alert])
    conn.execute("UPDATE incidents SET workflow_revision=workflow_revision+1, handling_note=? WHERE id=?",
                 ("" if clear_handling else incident["handling_note"], incident_id))
    after_incident = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
    append_history(conn, incident_id, action, stamp, operator, before, snapshot(after_incident, [after_alert]))


def apply(conn, incident_id, data, stamp, record_operation):
    """Validate all selected rows BEFORE mutation; caller owns an IMMEDIATE transaction."""
    incident = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
    if incident is None:
        raise HTTPException(404, "Incident not found")
    members = conn.execute("""SELECT a.* FROM incident_alerts m JOIN alerts a ON a.id=m.alert_id
        WHERE m.incident_id=? ORDER BY a.id""", (incident_id,)).fetchall()
    member_ids = {row["id"] for row in members}
    if data.expected_revision != incident["workflow_revision"] or set(data.expected_member_ids) != member_ids:
        raise HTTPException(409, "WORKFLOW_CHANGED")
    if not set(data.alert_ids) <= member_ids:
        raise HTTPException(422, "INVALID_SELECTION")
    selected = [row for row in members if row["id"] in data.alert_ids]
    handling = incident["handling_note"] if data.handling_note is None else data.handling_note.strip()
    if data.operation == "FINISH":
        if any(row["status"] != "ACKNOWLEDGED" for row in selected):
            raise HTTPException(422, "REVIEW_REQUIRED")
        if data.category not in {"SIMULATED_TEST", "SUSPECTED_FALSE_POSITIVE", "OTHER"}:
            raise HTTPException(422, "VERIFICATION_REQUIRED")
        if not data.note.strip():
            raise HTTPException(422, "NOTE_REQUIRED")
        if any(row["alarm_required"] for row in members) and not handling:
            raise HTTPException(422, "HANDLING_REQUIRED")
    if data.operation == "UNREVIEW" and any(row["processing_complete"] for row in selected):
        raise HTTPException(422, "REOPEN_REQUIRED")
    before = snapshot(incident, selected)
    changed = False
    new_alarm = data.operation == "ALARM_MARK" and any(not row["alarm_required"] for row in selected)
    if new_alarm:
        handling = ""  # A fresh need-to-alarm decision must not reuse old handling evidence.
    for row in selected:
        values = dict(row)
        operation = data.operation
        if operation in {"REVIEW", "UNREVIEW"}:
            target = "ACKNOWLEDGED" if operation == "REVIEW" else "OPEN"
            if row["status"] != target:
                values.update(status=target, acknowledged_at=stamp if target == "ACKNOWLEDGED" else None,
                              acknowledged_by=data.updated_by if target == "ACKNOWLEDGED" else None)
        elif operation in {"SAVE", "FINISH"}:
            if row["judgment_category"] != data.category or row["judgment_note"] != data.note:
                values.update(judgment_category=data.category, judgment_note=data.note,
                              note_revision=row["note_revision"]+1, note_updated_at=stamp, note_updated_by=data.updated_by,
                              processing_complete=0, processed_at=None, processed_by=None)
            if operation == "FINISH" and not values["processing_complete"]:
                values.update(processing_complete=1, processed_at=stamp, processed_by=data.updated_by)
        elif operation == "REOPEN" and row["processing_complete"]:
            values.update(processing_complete=0, processed_at=None, processed_by=None)
        elif operation in {"ALARM_MARK", "ALARM_CLEAR"}:
            required = int(operation == "ALARM_MARK")
            if row["alarm_required"] != required:
                values.update(alarm_required=required, alarm_marked_at=stamp, alarm_marked_by=data.updated_by)
                if required:
                    values.update(processing_complete=0, processed_at=None, processed_by=None)
        changes = {key: values[key] for key in HUMAN_FIELDS if values[key] != row[key]}
        if changes:
            columns = ",".join(f"{key}=?" for key in changes)
            conn.execute(f"UPDATE alerts SET {columns} WHERE id=?", (*changes.values(), row["id"]))
            after_row = conn.execute("SELECT * FROM alerts WHERE id=?", (row["id"],)).fetchone()
            record_operation(conn, row["id"], operation, stamp, data.updated_by, row, after_row)
            changed = True
    if handling != incident["handling_note"]:
        changed = True
    if changed:
        conn.execute("UPDATE incidents SET workflow_revision=workflow_revision+1,handling_note=? WHERE id=?",
                     (handling, incident_id))
        after_incident = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        after_rows = [conn.execute("SELECT * FROM alerts WHERE id=?", (row["id"],)).fetchone() for row in selected]
        append_history(conn, incident_id, data.operation, stamp, data.updated_by, before, snapshot(after_incident, after_rows))
    return "updated" if changed else "unchanged"
