"""Conservative static safety checks for public GPA workflow packages."""
from __future__ import annotations

import re
import zipfile
from pathlib import PurePosixPath

MAX_SCANNED_TEXT_BYTES = 8 * 1024 * 1024
TEXT_SUFFIXES = {".json", ".yaml", ".yml", ".txt", ".md"}
SECRET_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("openai_api_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("github_token", re.compile(r"\bgh[oprsu]_[A-Za-z0-9]{30,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("credential_url", re.compile(r"https?://[^\s/:]+:[^\s/@]+@", re.IGNORECASE)),
)
DANGEROUS_ACTION_PATTERN = re.compile(
    r"(?:action_type|type)\s*:\s*(?:shell|command|exec|execute_code|powershell|terminal)\b",
    re.IGNORECASE,
)


def scan_workflow_package(package_path) -> dict:
    """Return redacted findings; never include the matched secret text."""
    findings: list[dict] = []
    scanned_bytes = 0
    with zipfile.ZipFile(package_path) as archive:
        for info in archive.infolist():
            if info.is_dir() or PurePosixPath(info.filename).suffix.casefold() not in TEXT_SUFFIXES:
                continue
            if info.file_size > MAX_SCANNED_TEXT_BYTES:
                continue
            scanned_bytes += info.file_size
            if scanned_bytes > MAX_SCANNED_TEXT_BYTES:
                break
            try:
                text = archive.read(info).decode("utf-8")
            except (KeyError, UnicodeDecodeError):
                continue
            for finding_type, pattern in SECRET_PATTERNS:
                if pattern.search(text):
                    findings.append({
                        "severity": "block",
                        "type": finding_type,
                        "path": info.filename,
                        "message": "Potential credential material must be removed before publishing.",
                    })
            if info.filename.endswith(("workflow.yaml", "workflow.yml")) and DANGEROUS_ACTION_PATTERN.search(text):
                findings.append({
                    "severity": "block",
                    "type": "dangerous_execution_action",
                    "path": info.filename,
                    "message": "Arbitrary command execution is not allowed in public Replay packages.",
                })
    unique = []
    seen = set()
    for finding in findings:
        key = (finding["type"], finding["path"])
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return {
        "schema": "gpa.community-safety-scan/v1",
        "passed": not unique,
        "finding_count": len(unique),
        "findings": unique,
        "scanned_bytes": scanned_bytes,
    }


def require_safe_workflow_package(package_path) -> dict:
    scan = scan_workflow_package(package_path)
    if not scan["passed"]:
        kinds = ", ".join(sorted({item["type"] for item in scan["findings"]}))
        raise ValueError(f"Community safety scan blocked this package: {kinds}.")
    return scan
