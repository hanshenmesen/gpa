"""Validate public project metadata and local links before a GitHub release."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DOCUMENTS = (
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "SUPPORT.md",
    "GOVERNANCE.md",
    "ROADMAP.md",
    "CHANGELOG.md",
    "docs/feedback_program.md",
)
REQUIRED_FILES = (
    "LICENSE",
    "NOTICE",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/real_world_workflow.yml",
    ".github/ISSUE_TEMPLATE/compatibility_report.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/release-preview.yml",
    ".github/workflows/codeql.yml",
)
MARKDOWN_LINK = re.compile(r"!?(?:\[[^]]*\])\(([^)]+)\)")


def local_markdown_links(path: Path) -> list[Path]:
    links: list[Path] = []
    for raw_target in MARKDOWN_LINK.findall(path.read_text(encoding="utf-8")):
        target = raw_target.strip().split(" ", 1)[0].strip("<>")
        if not target or target.startswith(("#", "http://", "https://", "mailto:")):
            continue
        relative = unquote(target.split("#", 1)[0])
        if relative:
            links.append((path.parent / relative).resolve())
    return links


def validate_public_project(root: Path = ROOT) -> list[str]:
    root = root.resolve()
    errors: list[str] = []
    for relative in (*PUBLIC_DOCUMENTS, *REQUIRED_FILES):
        if not (root / relative).is_file():
            errors.append(f"missing public project file: {relative}")

    for relative in PUBLIC_DOCUMENTS:
        path = root / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if "/Users/" in text or "C:\\Users\\" in text:
            errors.append(f"personal absolute path in {relative}")
        for target in local_markdown_links(path):
            if not target.exists():
                errors.append(f"broken local link in {relative}: {target.relative_to(root)}")

    license_path = root / "LICENSE"
    if license_path.is_file() and "Apache License" not in license_path.read_text(encoding="utf-8"):
        errors.append("LICENSE is not Apache-2.0 text")
    return errors


def main() -> int:
    errors = validate_public_project()
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(
        f"Validated {len(PUBLIC_DOCUMENTS)} public documents and "
        f"{len(REQUIRED_FILES)} project files."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
