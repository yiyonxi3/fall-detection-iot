"""Run end-to-end acceptance checks against a local or deployed API."""

import argparse
import json
import sys
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def call(base_url, path, *, method="GET", body=None, api_key=None):
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["X-API-Key"] = api_key
    request = Request(base_url.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else None
    except HTTPError as error:
        raw = error.read().decode("utf-8")
        return error.code, json.loads(raw) if raw else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", default="local-test-key")
    args = parser.parse_args()

    suffix = uuid.uuid4().hex[:8]
    device_id = f"nano-accept-{suffix}"
    session_id = f"acceptance-{suffix}"
    common = {
        "device_id": device_id,
        "session_id": session_id,
        "uptime_ms": 12000,
        "model_version": "acceptance-model-v1",
        "pipeline_version": "acceptance-pipeline-v1",
        "threshold": 0.65,
        "is_test": True,
    }

    code, _ = call(args.base_url, "/api/v1/health")
    assert code == 200, f"health expected 200, got {code}"

    normal = common | {"sequence": 1, "fall_probability": 0.12, "state": "NORMAL"}
    code, accepted = call(args.base_url, "/api/v1/telemetry", method="POST", body=normal, api_key=args.api_key)
    assert code == 201 and accepted["status"] == "accepted" and not accepted["alert_created"]

    code, duplicate = call(args.base_url, "/api/v1/telemetry", method="POST", body=normal, api_key=args.api_key)
    assert code == 200 and duplicate["status"] == "duplicate"

    alarm = common | {"sequence": 2, "uptime_ms": 13000, "fall_probability": 0.88, "state": "NEW_ALARM"}
    code, created = call(args.base_url, "/api/v1/telemetry", method="POST", body=alarm, api_key=args.api_key)
    assert code == 201 and created["alert_created"]

    positive = common | {"sequence": 3, "uptime_ms": 14000, "fall_probability": 0.82, "state": "POSITIVE"}
    code, continued = call(args.base_url, "/api/v1/telemetry", method="POST", body=positive, api_key=args.api_key)
    assert code == 201 and not continued["alert_created"]

    code, alerts = call(args.base_url, "/api/v1/alerts?limit=200")
    matching = [item for item in alerts if item["device_id"] == device_id]
    assert code == 200 and len(matching) == 1, "NEW_ALARM/POSITIVE alert count is incorrect"

    alert_id = matching[0]["id"]
    code, context = call(
        args.base_url,
        f"/api/v1/alerts/{alert_id}/context?before=5&after=5",
    )
    assert code == 200, f"alert context expected 200, got {code}"
    assert context["alert"]["id"] == alert_id
    assert context["trigger_event"]["state"] == "NEW_ALARM"
    assert context["trigger_event"]["device_id"] == device_id
    assert [item["sequence"] for item in context["context_events"]] == [1, 2, 3]
    assert [item["relation"] for item in context["context_events"]] == ["before", "trigger", "after"]
    assert all(item["device_id"] == device_id for item in context["context_events"])

    code, _ = call(args.base_url, "/api/v1/alerts/999999999/context")
    assert code == 404, f"missing alert context expected 404, got {code}"

    bad = common | {"sequence": 4, "fall_probability": 0.10, "state": "NORMAL"}
    code, _ = call(args.base_url, "/api/v1/telemetry", method="POST", body=bad, api_key="wrong-key")
    assert code == 401, f"wrong API key expected 401, got {code}"

    code, latest = call(args.base_url, f"/api/v1/devices/{device_id}/latest")
    assert code == 200 and latest["detection_result"] == "FALL"

    review_path = f"/api/v1/alerts/{alert_id}/review"
    mark_path = f"/api/v1/alerts/{alert_id}/alarm-mark"
    actor = {"updated_by": "acceptance-test"}
    code, result = call(args.base_url, mark_path, method="PATCH",
                        body=actor | {"alarm_required": True}, api_key="wrong-key")
    assert code == 401
    code, result = call(args.base_url, review_path, method="PATCH",
                        body=actor | {"confirmed": True}, api_key="wrong-key")
    assert code == 401

    code, result = call(args.base_url, mark_path, method="PATCH",
                        body=actor | {"alarm_required": True}, api_key=args.api_key)
    assert code == 200 and result["alert"]["alarm_required"] is True
    assert result["alert"]["status"] == "OPEN", "Alarm marker changed review status"
    marked_at = result["alert"]["alarm_marked_at"]
    code, repeated = call(args.base_url, mark_path, method="PATCH",
                          body=actor | {"alarm_required": True}, api_key=args.api_key)
    assert code == 200 and repeated["status"] == "unchanged"
    assert repeated["alert"]["alarm_marked_at"] == marked_at

    code, result = call(args.base_url, review_path, method="PATCH",
                        body=actor | {"confirmed": True}, api_key=args.api_key)
    assert code == 200 and result["alert"]["status"] == "ACKNOWLEDGED"
    assert result["alert"]["alarm_required"] is True
    assert result["alert"]["acknowledged_by"] == "acceptance-test"
    code, result = call(args.base_url, review_path, method="PATCH",
                        body=actor | {"confirmed": False}, api_key=args.api_key)
    assert code == 200 and result["alert"]["status"] == "OPEN"
    assert result["alert"]["alarm_required"] is True
    assert result["alert"]["acknowledged_at"] is None
    code, result = call(args.base_url, mark_path, method="PATCH",
                        body=actor | {"alarm_required": False}, api_key=args.api_key)
    assert code == 200 and result["alert"]["alarm_required"] is False
    assert result["alert"]["status"] == "OPEN"

    code, saved = call(args.base_url, f"/api/v1/alerts/{alert_id}/context")
    assert code == 200 and saved["alert"]["alarm_required"] is False
    assert saved["trigger_event"]["state"] == "NEW_ALARM"
    assert saved["trigger_event"]["fall_probability"] == 0.88
    code, refreshed = call(args.base_url, "/api/v1/alerts?limit=200")
    assert code == 200
    stored = next(item for item in refreshed if item["id"] == alert_id)
    assert stored["status"] == "OPEN" and stored["alarm_required"] is False

    code, legacy = call(args.base_url, f"/api/v1/alerts/{alert_id}/acknowledge",
                        method="PATCH", body={"acknowledged_by": "acceptance-test"},
                        api_key=args.api_key)
    assert code == 200 and legacy["status"] == "acknowledged"
    code, legacy = call(args.base_url, f"/api/v1/alerts/{alert_id}/acknowledge",
                        method="PATCH", body={"acknowledged_by": "acceptance-test"},
                        api_key=args.api_key)
    assert code == 200 and legacy["status"] == "already_acknowledged"

    for path, body in [(review_path, actor | {"confirmed": "true"}),
                       (mark_path, actor | {"alarm_required": "true"})]:
        code, _ = call(args.base_url, path, method="PATCH", body=body, api_key=args.api_key)
        assert code == 422, "Boolean fields must reject string values"
    for route, body in [("review", actor | {"confirmed": True}),
                        ("alarm-mark", actor | {"alarm_required": True})]:
        code, _ = call(args.base_url, f"/api/v1/alerts/999999999/{route}",
                        method="PATCH", body=body, api_key=args.api_key)
        assert code == 404

    note_path = f"/api/v1/alerts/{alert_id}/note"
    note_body = actor | {"category": "SIMULATED_TEST", "note": "受控模拟测试，需组员核实。",
                         "expected_revision": 0}
    code, _ = call(args.base_url, note_path, method="PATCH", body=note_body, api_key="wrong-key")
    assert code == 401
    code, noted = call(args.base_url, note_path, method="PATCH", body=note_body, api_key=args.api_key)
    assert code == 200 and noted["alert"]["judgment_note"] == note_body["note"]
    assert noted["alert"]["note_revision"] == 1
    assert noted["alert"]["status"] == "ACKNOWLEDGED" and not noted["alert"]["alarm_required"]
    code, unchanged = call(args.base_url, note_path, method="PATCH", body=note_body, api_key=args.api_key)
    assert code == 200 and unchanged["status"] == "unchanged"
    code, _ = call(args.base_url, note_path, method="PATCH",
                   body=note_body | {"note": "过期的修改"}, api_key=args.api_key)
    assert code == 409, "A stale note must not overwrite newer work"
    for invalid in [note_body | {"category": "INVALID"}, note_body | {"note": "x" * 1001},
                    note_body | {"updated_by": "   "}, note_body | {"expected_revision": "1"}]:
        code, _ = call(args.base_url, note_path, method="PATCH", body=invalid, api_key=args.api_key)
        assert code == 422
    code, history = call(args.base_url, f"/api/v1/alerts/{alert_id}/operations")
    assert code == 200 and len(history["items"]) == 6, "Repeats/failures must not create history"
    assert [op["action"] for op in history["items"]] == ["NOTE", "REVIEW", "ALARM_MARK", "REVIEW", "REVIEW", "ALARM_MARK"]
    assert history["items"][0]["before"]["judgment_note"] == ""
    assert history["items"][0]["after"]["judgment_note"] == note_body["note"]
    assert history["items"][3]["before"]["status"] == "ACKNOWLEDGED"
    assert history["items"][3]["after"]["status"] == "OPEN", "Undo must remain in history"
    assert all(op["operated_by"] == "acceptance-test" for op in history["items"])
    code, saved = call(args.base_url, f"/api/v1/alerts/{alert_id}/context")
    assert code == 200 and saved["operations_total"] == 6
    assert saved["operations"] == history["items"]
    assert saved["alert"]["judgment_category"] == "SIMULATED_TEST"
    print("PASS: telemetry, deduplication, context, auth, independent marks, notes, revision conflict, operation history, undo, persistence, legacy compatibility")
    print(f"Dashboard: {args.base_url.rstrip('/')}/dashboard")
    print(f"Docs: {args.base_url.rstrip('/')}/docs")
    print(f"Upload: {args.base_url.rstrip('/')}/api/v1/telemetry")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, ValueError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        raise SystemExit(1)
