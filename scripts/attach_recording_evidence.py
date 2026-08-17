#!/usr/bin/env python3
"""Attach a decoded, privacy-reviewed recording to an existing GPA Workflow."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gpa.recording.evidence import attach_recording_evidence  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--recording", required=True)
    parser.add_argument("--capture-scope", required=True)
    parser.add_argument("--capture-method", required=True)
    parser.add_argument("--privacy-reviewed", action="store_true")
    parser.add_argument("--privacy-note", required=True)
    parser.add_argument("--source-run-id", default="")
    parser.add_argument("--browser-family", default="")
    parser.add_argument("--source-trace")
    args = parser.parse_args()
    result = attach_recording_evidence(
        args.workflow_id,
        args.recording,
        capture_scope=args.capture_scope,
        capture_method=args.capture_method,
        privacy_reviewed=args.privacy_reviewed,
        privacy_note=args.privacy_note,
        source_run_id=args.source_run_id,
        browser_family=args.browser_family,
        source_trace_path=args.source_trace,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
