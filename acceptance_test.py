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

    bad = common | {"sequence": 4, "fall_probability": 0.10, "state": "NORMAL"}
    code, _ = call(args.base_url, "/api/v1/telemetry", method="POST", body=bad, api_key="wrong-key")
    assert code == 401, f"wrong API key expected 401, got {code}"

    code, latest = call(args.base_url, f"/api/v1/devices/{device_id}/latest")
    assert code == 200 and latest["detection_result"] == "FALL"

    print("PASS: health, accepted, duplicate, NEW_ALARM, POSITIVE, API key, device status")
    print(f"Dashboard: {args.base_url.rstrip('/')}/dashboard")
    print(f"Docs: {args.base_url.rstrip('/')}/docs")
    print(f"Upload: {args.base_url.rstrip('/')}/api/v1/telemetry")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, ValueError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        raise SystemExit(1)
