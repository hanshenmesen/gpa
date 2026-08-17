#!/usr/bin/env python3
"""Render a verified source-evidence video from a successful Safe Web run."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gpa.recording.source_evidence import build_safe_web_source_evidence  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--trace-output")
    args = parser.parse_args()
    result = build_safe_web_source_evidence(
        args.workflow_id,
        args.run_id,
        args.output,
        trace_destination=args.trace_output,
    )
    print(json.dumps({
        "video_path": result["video_path"],
        "trace_path": result["trace_path"],
        "page_count": result["trace"]["page_count"],
        "steps_run": result["trace"]["steps_run"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
