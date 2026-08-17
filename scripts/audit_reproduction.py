#!/usr/bin/env python3
"""Export a workflow and audit it from an isolated Agent workspace."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gpa.community.package import export_workflow_package  # noqa: E402
from gpa.replay.audit import audit_reproduction_package  # noqa: E402
from gpa.replay.environment import capture_environment  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow_id", help="Local workflow to export and audit")
    parser.add_argument("--target-environment", type=Path, help="Optional JSON target environment")
    parser.add_argument("--no-execute", action="store_true", help="Inspect/import without Safe Web execution")
    parser.add_argument("--output", type=Path, help="Write the JSON report to this file")
    args = parser.parse_args()

    if args.target_environment:
        target = json.loads(args.target_environment.read_text(encoding="utf-8"))
    else:
        target = capture_environment()
    with tempfile.TemporaryDirectory(prefix="gpa-reproduction-package-") as temporary:
        package = export_workflow_package(args.workflow_id, Path(temporary))
        report = audit_reproduction_package(
            package,
            target_environment=target,
            execute=not args.no_execute,
        )
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if report["status"] == "passed" or args.no_execute else 1


if __name__ == "__main__":
    raise SystemExit(main())
