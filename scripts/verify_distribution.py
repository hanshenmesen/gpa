"""Validate that a built GPA wheel contains its required runtime assets."""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

REQUIRED_MEMBERS = {
    "demo_web/__init__.py",
    "demo_web/case_lab.html",
    "demo_web/control.html",
    "demo_web/community.html",
    "demo_web/index.html",
    "demo_web/environment.js",
    "demo_web/product.css",
    "demo_web/product.js",
    "demo_web/server.py",
    "demo_web/store.html",
    "gpa/desktop/__init__.py",
    "gpa/desktop/app.py",
    "gpa/cloud_server/app.py",
    "gpa/cloud_server/operations.py",
    "gpa/cloud_server/auth.py",
    "gpa/cloud_server/migrations/0001_initial.sql",
    "gpa/cloud_server/migrations/0002_security_and_operations.sql",
    "gpa/cloud_server/migrations.py",
    "gpa/cloud_server/pairing.py",
    "gpa/cloud_server/preflight.py",
    "gpa/storage/__init__.py",
    "gpa/storage/workflow.py",
    "gpa/diagnostics.py",
    "gpa/update.py",
    "gpa/execution/decision_policy.py",
    "gpa/replay/evidence.py",
    "gpa/replay/client_lease.py",
    "gpa/replay/environment.py",
    "gpa/replay/gate.py",
    "gpa/replay/request.py",
    "gpa/replay/understanding.py",
    "gpa/replay/worker_protocol.py",
}
REQUIRED_ENTRY_POINTS = {
    "gpa = gpa.integration.cli:main",
    "gpa-cloud = gpa.cloud_server.app:main",
    "gpa-cloud-migrate = gpa.cloud_server.migrations:main",
    "gpa-desktop = gpa.desktop.app:main",
    "gpa-release-preflight = gpa.cloud_server.preflight:main",
    "gpa-web = demo_web.server:main",
}


def find_wheel(path: Path) -> Path:
    """Resolve *path* to exactly one wheel file."""
    if path.is_file():
        if path.suffix != ".whl":
            raise ValueError(f"not a wheel file: {path}")
        return path

    wheels = sorted(path.glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError(f"expected exactly one wheel in {path}, found {len(wheels)}")
    return wheels[0]


def verify_wheel(wheel: Path) -> None:
    """Raise ValueError when required files or entry points are absent."""
    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())
        missing = sorted(REQUIRED_MEMBERS - members)
        if missing:
            raise ValueError(f"wheel is missing runtime files: {missing}")

        licenses = [
            name for name in members if name.endswith(".dist-info/licenses/LICENSE")
        ]
        if len(licenses) != 1:
            raise ValueError(f"expected one packaged Apache license, found {len(licenses)}")

        entry_point_files = [name for name in members if name.endswith(".dist-info/entry_points.txt")]
        if len(entry_point_files) != 1:
            raise ValueError(
                "expected exactly one .dist-info/entry_points.txt, "
                f"found {len(entry_point_files)}"
            )
        entry_points = archive.read(entry_point_files[0]).decode("utf-8")

    missing_entry_points = sorted(
        entry_point for entry_point in REQUIRED_ENTRY_POINTS if entry_point not in entry_points
    )
    if missing_entry_points:
        raise ValueError(f"wheel is missing console entry points: {missing_entry_points}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="wheel file or directory containing one wheel")
    args = parser.parse_args()

    try:
        wheel = find_wheel(args.path)
        verify_wheel(wheel)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))

    print(f"Validated GPA distribution: {wheel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
