"""Subprocess entry point for isolated package reproduction audits."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gpa.replay.audit import audit_reproduction_package


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path)
    args = parser.parse_args(argv)
    request = json.loads(sys.stdin.read() or "{}")
    target_environment = request.get("target_environment", {})
    if target_environment is None:
        target_environment = {}
    if not isinstance(target_environment, dict):
        raise TypeError("target_environment must be an object")
    report = audit_reproduction_package(
        args.package,
        target_environment=target_environment,
        execute=True,
    )
    json.dump(report, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
