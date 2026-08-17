#!/usr/bin/env python3
"""Exercise the real HTTP record/start -> record/stop lifecycle in isolation."""
from __future__ import annotations

import json
import threading
import urllib.request

import demo_web.server as server


def _post(base: str, path: str, body: dict) -> dict:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


def main() -> None:
    server.DESKTOP_AUTOMATION_REQUESTED = True
    server.DESKTOP_AUTOMATION_ENABLED = True
    server.RECORDING_PROCESS_ISOLATION = True
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        started = _post(base, "/api/record/start", {
            "workflow_id": "isolated_api_probe",
            "task_description": "Verify the isolated recorder HTTP lifecycle",
            "client_environment": {
                "language": "en-US",
                "timezone": "America/Recife",
                "screen": {"width": 1470, "height": 956, "pixel_ratio": 2},
                "browser": {"family": "Probe", "viewport_width": 1280, "viewport_height": 720},
            },
        })
        status_during = server._public_state()["recording"]
        stopped = _post(base, "/api/record/stop", {"build": False, "preview": False})
        status_after = server._public_state()["recording"]
        print(json.dumps({
            "ok": True,
            "started": started,
            "during": {
                "status": status_during["status"],
                "process_isolated": status_during["process_isolated"],
                "worker_pid": status_during["worker_pid"],
            },
            "stopped": stopped,
            "after": {
                "status": status_after["status"],
                "active": status_after["active"],
            },
        }))
    finally:
        server._abort_active_recording("Probe cleanup.")
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
