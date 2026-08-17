#!/usr/bin/env python3
"""Start and stop the isolated native recorder without building a Workflow."""
from __future__ import annotations

import argparse
import json
import time

from gpa.recording.worker_client import RecorderWorkerClient


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", default="quartz")
    parser.add_argument("--duration", type=float, default=0.25)
    args = parser.parse_args()

    client = RecorderWorkerClient(input_backend=args.backend)
    try:
        client.start()
        worker_pid = client.worker_pid
        time.sleep(max(0.0, min(args.duration, 5.0)))
        event_count = client.refresh_event_count()
        recording = client.stop()
        print(json.dumps({
            "ok": True,
            "worker_pid": worker_pid,
            "parent_isolated": True,
            "input_backend": args.backend,
            "event_count": event_count,
            "snapshot_event_count": len(recording.events),
        }))
    finally:
        client.close()


if __name__ == "__main__":
    main()
