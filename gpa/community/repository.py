"""Local community catalog for portable GPA workflow records."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from gpa.community.package import (
    DEFAULT_MAX_PACKAGE_BYTES,
    PackageImportResult,
    import_workflow_package,
    inspect_workflow_package,
)
from gpa.community.safety import require_safe_workflow_package
from gpa.replay.understanding import build_reproduction_contract
from gpa.storage.workflow import WorkflowStorage

RECORD_SCHEMA_VERSION = "1.0"
PACKAGE_FILENAME = "package.gpa-record.zip"
RECORD_FILENAME = "record.json"
FEEDBACK_FILENAME = "feedback.jsonl"
REPORTS_FILENAME = "reports.jsonl"
APPEALS_FILENAME = "appeals.jsonl"
MODERATION_AUDIT_FILENAME = "moderation_audit.jsonl"
SAVED_FILENAME = "saved_records.json"
ALLOWED_LICENSES = {"CC-BY-4.0", "CC0-1.0", "MIT", "Apache-2.0"}
REPORT_CATEGORIES = {
    "dangerous_automation",
    "malware",
    "privacy_or_credentials",
    "copyright",
    "impersonation",
    "misleading",
    "spam",
    "other",
}
HIGH_SEVERITY_REPORT_CATEGORIES = {
    "dangerous_automation",
    "malware",
    "privacy_or_credentials",
}
MODERATION_STATUSES = {"published", "under_review", "restricted", "removed"}
_RECORD_ID_RE = re.compile(r"rec_[a-f0-9]{16}")
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


def _lock_for(root: Path) -> threading.RLock:
    key = str(root.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, payload: dict) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _content_fingerprint(manifest: dict) -> str:
    files = sorted(
        (str(item.get("path")), str(item.get("sha256")))
        for item in manifest.get("files", [])
        if isinstance(item, dict)
    )
    raw = json.dumps(
        {"format_version": manifest.get("format_version"), "files": files},
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _semantic_workflow_payload(raw: object) -> dict:
    """Return the portable task definition, excluding host/session evidence."""
    if not isinstance(raw, dict):
        return {}
    payload = dict(raw)
    payload.pop("workflow_id", None)
    for field in ("provenance", "environment", "understanding", "artifacts"):
        payload.pop(field, None)
    return payload


def _package_matches_existing_source(package_path: Path, storage: WorkflowStorage) -> str:
    """Find an unchanged source Replay without importing a second copy.

    Community packages intentionally carry environment and evidence captured on
    another host.  Those fields must not make an otherwise identical task look
    like a different Replay in the local library.
    """
    manifest = inspect_workflow_package(package_path)
    source_id = str(manifest.get("workflow_id") or "").strip()
    if not source_id:
        return ""
    try:
        workflow, _ = storage.load(source_id)
    except (FileNotFoundError, ValueError, KeyError):
        return ""
    try:
        with zipfile.ZipFile(package_path) as archive:
            packaged = yaml.safe_load(archive.read("workflow/workflow.yaml"))
    except (KeyError, OSError, ValueError, zipfile.BadZipFile, yaml.YAMLError):
        return ""
    local = workflow.to_yaml_dict()
    if _semantic_workflow_payload(packaged) != _semantic_workflow_payload(local):
        return ""
    return workflow.workflow_id


def _clean_text(value, *, limit: int, fallback: str = "") -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] or fallback


def _clean_tags(tags) -> list[str]:
    if not isinstance(tags, list):
        raise ValueError("tags must be a list.")
    cleaned: list[str] = []
    for value in tags[:10]:
        tag = _clean_text(value, limit=32).casefold()
        if tag and tag not in cleaned:
            cleaned.append(tag)
    return cleaned


class CommunityRepository:
    """Filesystem-backed community records with atomic catalog updates."""

    def __init__(self, root: str | Path, *, max_package_bytes: int = DEFAULT_MAX_PACKAGE_BYTES):
        self.root = Path(root)
        self.records_dir = self.root / "records"
        self.max_package_bytes = max_package_bytes
        self._lock = _lock_for(self.root)

    def publish_package(
        self,
        package: bytes,
        *,
        author: str,
        tags: list[str],
        license_id: str,
        privacy_reviewed: bool,
        recording_verification: dict | None = None,
        publisher_declaration: dict | None = None,
    ) -> dict:
        if privacy_reviewed is not True:
            raise ValueError("Explicit privacy review is required before publishing.")
        author = _clean_text(author, limit=80, fallback="Anonymous")
        tags = _clean_tags(tags)
        license_id = str(license_id or "").strip()
        recording_verification = dict(recording_verification or {})
        publisher_declaration = self._clean_publisher_declaration(publisher_declaration)
        if license_id not in ALLOWED_LICENSES:
            raise ValueError(f"Unsupported record license: {license_id}")

        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".community-publish-", dir=self.root))
        staged_package = staging / PACKAGE_FILENAME
        try:
            if not isinstance(package, bytes):
                raise TypeError("Community packages must be supplied as an immutable byte snapshot.")
            if len(package) > self.max_package_bytes:
                raise ValueError("Package exceeds maximum package size.")
            staged_package.write_bytes(package)

            manifest = inspect_workflow_package(
                staged_package,
                max_package_bytes=self.max_package_bytes,
            )
            scan_result = require_safe_workflow_package(staged_package)
            safety_scan = {
                "schema": "gpa.community-safety-scan/v1",
                "passed": True,
                "finding_count": 0,
                "findings": [],
                "scanned_bytes": int(scan_result.get("scanned_bytes") or 0),
            }
            fingerprint = _content_fingerprint(manifest)
            package_sha256 = _sha256(staged_package)
            now = _utc_now()

            with self._lock:
                self.records_dir.mkdir(parents=True, exist_ok=True)
                existing = self._find_by_fingerprint_locked(fingerprint)
                if existing is not None:
                    if recording_verification.get("verified") is True:
                        existing["recording_verification"] = recording_verification
                    existing["updated_at"] = now
                    existing["reproduction_contract"] = build_reproduction_contract(
                        step_count=int(existing.get("step_count") or 0),
                        environment=dict(existing.get("environment") or {}),
                        understanding=dict(existing.get("understanding") or {}),
                        artifacts=dict(existing.get("artifacts") or {}),
                        recording_verified=(
                            existing.get("recording_verification") or {}
                        ).get("verified") is True,
                    )
                    _atomic_json(
                        self.records_dir / existing["record_id"] / RECORD_FILENAME,
                        existing,
                    )
                    return {**existing, "duplicate": True}

                record_id = f"rec_{uuid.uuid4().hex[:16]}"
                record_dir = self.records_dir / record_id
                staged_record_dir = staging / record_id
                staged_record_dir.mkdir()
                os.replace(staged_package, staged_record_dir / PACKAGE_FILENAME)
                record = {
                    "schema_version": RECORD_SCHEMA_VERSION,
                    "record_id": record_id,
                    "published_at": now,
                    "updated_at": now,
                    "author": author,
                    "tags": tags,
                    "record_license": license_id,
                    "privacy_reviewed": True,
                    "publisher_declaration": publisher_declaration,
                    "safety_scan": safety_scan,
                    "moderation": {
                        "status": "published",
                        "trust_tier": "automated_checks",
                        "report_count": 0,
                        "open_report_count": 0,
                        "last_reviewed_at": "",
                        "reason": "",
                    },
                    "content_fingerprint": fingerprint,
                    "package_sha256": package_sha256,
                    "package_bytes": (staged_record_dir / PACKAGE_FILENAME).stat().st_size,
                    "workflow_id": str(manifest.get("workflow_id") or ""),
                    "workflow_name": str(manifest.get("workflow_name") or ""),
                    "workflow_title": str(manifest.get("workflow_title") or ""),
                    "description": str(manifest.get("description") or ""),
                    "task_description": str(manifest.get("task_description") or ""),
                    "provenance": dict(manifest.get("provenance") or {}),
                    "environment": dict(manifest.get("environment") or {}),
                    "understanding": dict(manifest.get("understanding") or {}),
                    "artifacts": dict(manifest.get("artifacts") or {}),
                    "recording_verification": recording_verification,
                    "step_count": int(manifest.get("step_count") or 0),
                    "variable_names": list(manifest.get("variable_names") or []),
                    "platform": dict(manifest.get("platform") or {}),
                    "compatibility": dict(manifest.get("compatibility") or {}),
                    "reproduction_contract": build_reproduction_contract(
                        step_count=int(manifest.get("step_count") or 0),
                        environment=dict(manifest.get("environment") or {}),
                        understanding=dict(manifest.get("understanding") or {}),
                        artifacts=dict(manifest.get("artifacts") or {}),
                        # A declared recording is evidence only after the isolated
                        # decoder has explicitly verified it.  Missing probe data
                        # must never be interpreted as a successful verification.
                        recording_verified=recording_verification.get("verified") is True,
                    ),
                    "stats": self._empty_stats(),
                }
                _atomic_json(staged_record_dir / RECORD_FILENAME, record)
                (staged_record_dir / FEEDBACK_FILENAME).touch()
                os.replace(staged_record_dir, record_dir)
                return {**record, "duplicate": False}
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def list_records(self, *, query: str = "", tag: str = "") -> list[dict]:
        query = _clean_text(query, limit=120).casefold()
        tag = _clean_text(tag, limit=32).casefold()
        with self._lock:
            records = self._read_all_locked()
            saved = self._read_saved_locked()
        filtered = []
        for record in records:
            if self._moderation_status(record) in {"restricted", "removed"}:
                continue
            haystack = " ".join(
                [
                    record.get("record_id", ""),
                    record.get("workflow_name", ""),
                    record.get("workflow_title", ""),
                    record.get("description", ""),
                    record.get("task_description", ""),
                    json.dumps(record.get("provenance") or {}, ensure_ascii=False),
                    json.dumps(record.get("understanding") or {}, ensure_ascii=False),
                    record.get("author", ""),
                    " ".join(record.get("tags") or []),
                ]
            ).casefold()
            if query and query not in haystack:
                continue
            if tag and tag not in (record.get("tags") or []):
                continue
            summary = self._summary(record)
            summary["saved_workflow_id"] = self._saved_workflow_id(saved, record["record_id"])
            filtered.append(summary)
        # Maintained records are republished when evidence improves. Prefer the
        # most recently verified revision, not a newer but poorer seed that
        # happened to receive a later initial publication timestamp.
        ordered = sorted(
            filtered,
            key=lambda item: item.get("updated_at") or item.get("published_at", ""),
            reverse=True,
        )
        visible = []
        seeded_workflows = set()
        for item in ordered:
            legacy_misclassified = (
                item.get("author") == "GPA Verified Runs"
                and "real-task" in (item.get("tags") or [])
            )
            if legacy_misclassified:
                continue
            maintained = (
                item.get("author") in {
                    "GPA Engineering",
                    "AssistantBench · GPA reproduction",
                }
                and bool(
                    {"benchmark-task", "internal-regression"}
                    & set(item.get("tags") or [])
                )
            )
            seeded_example = (
                item.get("author") == "GPA Examples"
                and bool({"demo", "case"} & set(item.get("tags") or []))
            )
            workflow_id = str(item.get("workflow_id") or "")
            seeded = maintained or seeded_example
            if seeded and workflow_id in seeded_workflows:
                continue
            if seeded and workflow_id:
                seeded_workflows.add(workflow_id)
            visible.append(item)
        return visible

    def get_record(self, record_id: str, *, include_feedback: bool = False) -> dict:
        record_id = self._safe_record_id(record_id)
        with self._lock:
            record = self._read_record_locked(record_id)
            feedback = self._read_feedback_locked(record_id)
            record = self._reconcile_feedback_stats_locked(record_id, record, feedback)
            result = dict(record)
            result["saved_workflow_id"] = self._saved_workflow_id(
                self._read_saved_locked(),
                record_id,
            )
            if include_feedback:
                result["recent_feedback"] = feedback[-10:][::-1]
            return result

    def package_path(self, record_id: str) -> Path:
        record_id = self._safe_record_id(record_id)
        path = self.records_dir / record_id / PACKAGE_FILENAME
        if not path.is_file():
            raise FileNotFoundError(f"Community record not found: {record_id}")
        return path

    def register_download(self, record_id: str) -> dict:
        return self._increment_stat(record_id, "downloads")

    def store_isolated_reproduction_audit(self, record_id: str, audit: dict) -> dict:
        """Persist a verified audit summary separately from package identity."""
        record_id = self._safe_record_id(record_id)
        if not isinstance(audit, dict) or audit.get("schema") != "gpa.isolated-reproduction-audit-summary/v1":
            raise ValueError("Invalid isolated reproduction audit summary.")
        package_sha256 = str(audit.get("package_sha256") or "")
        if not re.fullmatch(r"[a-f0-9]{64}", package_sha256):
            raise ValueError("Invalid isolated reproduction audit package checksum.")
        if audit.get("status") not in {"passed", "failed", "not_run"}:
            raise ValueError("Invalid isolated reproduction audit status.")
        with self._lock:
            record = self._read_record_locked(record_id)
            if package_sha256 != str(record.get("package_sha256") or ""):
                raise ValueError("Isolated audit package checksum does not match the published record.")
            record["isolated_reproduction_audit"] = dict(audit)
            record["updated_at"] = _utc_now()
            _atomic_json(self.records_dir / record_id / RECORD_FILENAME, record)
            return dict(record["isolated_reproduction_audit"])

    def import_record(
        self,
        record_id: str,
        *,
        workflow_id: Optional[str] = None,
        storage: WorkflowStorage,
    ) -> PackageImportResult:
        record_id = self._safe_record_id(record_id)
        with self._lock:
            record = self._read_record_locked(record_id)
            self._ensure_record_available(record, "imported")
            recording = dict(((record.get("artifacts") or {}).get("recording") or {}))
            privacy_review = dict(recording.get("privacy_review") or {})
            if privacy_review.get("status") == "failed" or privacy_review.get("quarantined") is True:
                raise PermissionError(
                    "Privacy-quarantined recordings cannot be imported into another Agent workspace."
                )
            saved = self._read_saved_locked()
            existing_id = self._saved_workflow_id(saved, record_id)
            if existing_id:
                try:
                    workflow, _ = storage.load(existing_id)
                except (FileNotFoundError, ValueError, KeyError):
                    saved.pop(record_id, None)
                    _atomic_json(self.root / SAVED_FILENAME, saved)
                else:
                    return PackageImportResult(
                        workflow_id=workflow.workflow_id,
                        workflow_name=workflow.workflow_name,
                        storage_dir=workflow.storage_dir,
                        was_renamed=False,
                        already_saved=True,
                    )

            path = self.package_path(record_id)
            if workflow_id is None:
                source_id = _package_matches_existing_source(path, storage)
                if source_id:
                    workflow, _ = storage.load(source_id)
                    saved[record_id] = {
                        "workflow_id": source_id,
                        "saved_at": _utc_now(),
                    }
                    _atomic_json(self.root / SAVED_FILENAME, saved)
                    return PackageImportResult(
                        workflow_id=workflow.workflow_id,
                        workflow_name=workflow.workflow_name,
                        storage_dir=workflow.storage_dir,
                        was_renamed=False,
                        already_saved=True,
                    )
            result = import_workflow_package(
                path,
                workflow_id=workflow_id,
                overwrite=False,
                storage=storage,
            )
            try:
                imported_workflow, imported_subgraphs = storage.load(result.workflow_id)
                imported_workflow.provenance = {
                    **dict(imported_workflow.provenance or {}),
                    "community_import": {
                        "record_id": record_id,
                        "package_sha256": str(record.get("package_sha256") or ""),
                        "imported_at": _utc_now(),
                        "source_workflow_id": str(record.get("workflow_id") or ""),
                        "named_copy": workflow_id is not None,
                    },
                }
                storage.save(imported_workflow, imported_subgraphs)
                saved[record_id] = {
                    "workflow_id": result.workflow_id,
                    "saved_at": _utc_now(),
                }
                _atomic_json(self.root / SAVED_FILENAME, saved)
                self._increment_stat(record_id, "imports")
            except Exception:
                saved.pop(record_id, None)
                try:
                    _atomic_json(self.root / SAVED_FILENAME, saved)
                finally:
                    storage.delete(result.workflow_id)
                raise
            return result

    def forget_saved_workflow(self, workflow_id: str) -> None:
        workflow_id = str(workflow_id or "").strip()
        if not workflow_id:
            return
        with self._lock:
            saved = self._read_saved_locked()
            filtered = {
                record_id: entry
                for record_id, entry in saved.items()
                if self._saved_workflow_id({record_id: entry}, record_id) != workflow_id
            }
            if filtered != saved:
                _atomic_json(self.root / SAVED_FILENAME, filtered)

    def remember_saved_workflow(
        self,
        record_id: str,
        workflow_id: str,
        *,
        storage: WorkflowStorage,
    ) -> None:
        """Link a published record to its existing local source workflow."""
        record_id = self._safe_record_id(record_id)
        workflow_id = str(workflow_id or "").strip()
        workflow, _ = storage.load(workflow_id)
        with self._lock:
            self._read_record_locked(record_id)
            saved = self._read_saved_locked()
            if self._saved_workflow_id(saved, record_id) == workflow.workflow_id:
                return
            saved[record_id] = {
                "workflow_id": workflow.workflow_id,
                "saved_at": _utc_now(),
            }
            _atomic_json(self.root / SAVED_FILENAME, saved)

    def add_feedback(
        self,
        record_id: str,
        *,
        success: bool,
        failed_step: Optional[int] = None,
        note: str = "",
        environment: Optional[dict] = None,
        feedback_id: str = "",
    ) -> dict:
        if not isinstance(success, bool):
            raise ValueError("success must be a boolean.")
        record_id = self._safe_record_id(record_id)
        feedback_id = feedback_id or f"fb_{uuid.uuid4().hex[:16]}"
        if not re.fullmatch(r"fb_[A-Za-z0-9_-]{8,64}", feedback_id):
            raise ValueError("Invalid feedback_id.")
        environment = self._clean_environment(environment or {})
        note = _clean_text(note, limit=500)

        with self._lock:
            record = self._read_record_locked(record_id)
            existing = self._read_feedback_locked(record_id)
            for item in existing:
                if item.get("feedback_id") == feedback_id:
                    self._reconcile_feedback_stats_locked(record_id, record, existing)
                    return {**item, "duplicate": True}
            if success and failed_step is not None:
                raise ValueError("Successful feedback cannot include failed_step.")
            if not success:
                if not isinstance(failed_step, int) or not 1 <= failed_step <= record["step_count"]:
                    raise ValueError("failed_step must identify a valid workflow step.")

            feedback = {
                "feedback_id": feedback_id,
                "record_id": record_id,
                "created_at": _utc_now(),
                "success": success,
                "failed_step": failed_step,
                "note": note,
                "environment": environment,
            }
            feedback_path = self.records_dir / record_id / FEEDBACK_FILENAME
            with feedback_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(feedback, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

            record["stats"] = self._feedback_stats(record, [*existing, feedback])
            record["updated_at"] = _utc_now()
            _atomic_json(self.records_dir / record_id / RECORD_FILENAME, record)
            return {**feedback, "duplicate": False}

    def submit_report(
        self,
        record_id: str,
        *,
        category: str,
        details: str = "",
        report_id: str = "",
    ) -> dict:
        """Persist an idempotent safety report and apply conservative triage."""
        record_id = self._safe_record_id(record_id)
        category = str(category or "").strip().casefold()
        if category not in REPORT_CATEGORIES:
            raise ValueError("Unsupported report category.")
        details = _clean_text(details, limit=1200)
        report_id = report_id or f"rpt_{uuid.uuid4().hex[:16]}"
        if not re.fullmatch(r"rpt_[A-Za-z0-9_-]{8,64}", report_id):
            raise ValueError("Invalid report_id.")
        with self._lock:
            record = self._read_record_locked(record_id)
            reports = self._read_jsonl_locked(record_id, REPORTS_FILENAME)
            for item in reports:
                if item.get("report_id") == report_id:
                    return {**item, "duplicate": True}
            now = _utc_now()
            report = {
                "report_id": report_id,
                "record_id": record_id,
                "created_at": now,
                "category": category,
                "details": details,
                "status": "open",
                "resolution": "",
            }
            self._append_jsonl_locked(record_id, REPORTS_FILENAME, report)
            moderation = self._moderation_payload(record)
            moderation["report_count"] = len(reports) + 1
            moderation["open_report_count"] = sum(
                item.get("status") == "open" for item in [*reports, report]
            )
            if category in HIGH_SEVERITY_REPORT_CATEGORIES:
                moderation.update({
                    "status": "restricted",
                    "reason": "A high-severity safety report requires operator review.",
                    "restricted_at": now,
                })
            elif moderation["open_report_count"] >= 3 and moderation["status"] == "published":
                moderation.update({
                    "status": "under_review",
                    "reason": "Multiple independent reports require operator review.",
                })
            record["moderation"] = moderation
            record["updated_at"] = now
            _atomic_json(self.records_dir / record_id / RECORD_FILENAME, record)
            self._append_moderation_audit_locked({
                "created_at": now,
                "record_id": record_id,
                "event": "report_received",
                "report_id": report_id,
                "category": category,
                "resulting_status": moderation["status"],
            })
            return {**report, "duplicate": False, "record_status": moderation["status"]}

    def submit_appeal(
        self,
        record_id: str,
        *,
        explanation: str,
        appeal_id: str = "",
    ) -> dict:
        record_id = self._safe_record_id(record_id)
        explanation = _clean_text(explanation, limit=1600)
        if len(explanation) < 20:
            raise ValueError("Appeal explanation must contain at least 20 characters.")
        appeal_id = appeal_id or f"apl_{uuid.uuid4().hex[:16]}"
        if not re.fullmatch(r"apl_[A-Za-z0-9_-]{8,64}", appeal_id):
            raise ValueError("Invalid appeal_id.")
        with self._lock:
            record = self._read_record_locked(record_id)
            moderation = self._moderation_payload(record)
            if moderation["status"] not in {"under_review", "restricted", "removed"}:
                raise ValueError("Only reviewed, restricted, or removed records can be appealed.")
            appeals = self._read_jsonl_locked(record_id, APPEALS_FILENAME)
            for item in appeals:
                if item.get("appeal_id") == appeal_id:
                    return {**item, "duplicate": True}
            appeal = {
                "appeal_id": appeal_id,
                "record_id": record_id,
                "created_at": _utc_now(),
                "explanation": explanation,
                "status": "open",
            }
            self._append_jsonl_locked(record_id, APPEALS_FILENAME, appeal)
            self._append_moderation_audit_locked({
                "created_at": appeal["created_at"],
                "record_id": record_id,
                "event": "appeal_received",
                "appeal_id": appeal_id,
            })
            return {**appeal, "duplicate": False}

    def moderate_record(self, record_id: str, *, action: str, reason: str) -> dict:
        record_id = self._safe_record_id(record_id)
        action = str(action or "").strip().casefold()
        next_status = {
            "approve": "published",
            "review": "under_review",
            "restrict": "restricted",
            "remove": "removed",
            "restore": "published",
        }.get(action)
        if not next_status:
            raise ValueError("Unsupported moderation action.")
        reason = _clean_text(reason, limit=800)
        if len(reason) < 8:
            raise ValueError("Moderation reason must contain at least 8 characters.")
        with self._lock:
            record = self._read_record_locked(record_id)
            moderation = self._moderation_payload(record)
            previous = moderation["status"]
            now = _utc_now()
            moderation.update({
                "status": next_status,
                "reason": reason,
                "last_reviewed_at": now,
                "trust_tier": "operator_reviewed" if next_status == "published" else moderation["trust_tier"],
            })
            if next_status == "published":
                reports = self._read_jsonl_locked(record_id, REPORTS_FILENAME)
                moderation["open_report_count"] = 0
                if reports:
                    resolved = []
                    for report in reports:
                        if report.get("status") == "open":
                            report = {**report, "status": "resolved", "resolution": reason, "resolved_at": now}
                        resolved.append(report)
                    self._write_jsonl_locked(record_id, REPORTS_FILENAME, resolved)
            record["moderation"] = moderation
            record["updated_at"] = now
            _atomic_json(self.records_dir / record_id / RECORD_FILENAME, record)
            self._append_moderation_audit_locked({
                "created_at": now,
                "record_id": record_id,
                "event": "moderation_action",
                "action": action,
                "previous_status": previous,
                "resulting_status": next_status,
                "reason": reason,
            })
            return dict(record)

    def moderation_overview(self) -> dict:
        with self._lock:
            records = self._read_all_locked()
            recent_reports = []
            recent_appeals = []
            for record in records:
                record_id = str(record.get("record_id") or "")
                recent_reports.extend(self._read_jsonl_locked(record_id, REPORTS_FILENAME))
                recent_appeals.extend(self._read_jsonl_locked(record_id, APPEALS_FILENAME))
        statuses = {status: 0 for status in MODERATION_STATUSES}
        trust_tiers: dict[str, int] = {}
        for record in records:
            moderation = self._moderation_payload(record)
            statuses[moderation["status"]] += 1
            tier = moderation["trust_tier"]
            trust_tiers[tier] = trust_tiers.get(tier, 0) + 1
        open_reports = [item for item in recent_reports if item.get("status") == "open"]
        category_counts = {category: 0 for category in sorted(REPORT_CATEGORIES)}
        for item in open_reports:
            category = str(item.get("category") or "other")
            category_counts[category] = category_counts.get(category, 0) + 1
        return {
            "schema": "gpa.community-moderation-overview/v1",
            "generated_at": _utc_now(),
            "records": {"total": len(records), "by_status": statuses, "by_trust_tier": trust_tiers},
            "reports": {
                "total": len(recent_reports),
                "open": len(open_reports),
                "by_category": category_counts,
                "recent": sorted(recent_reports, key=lambda item: item.get("created_at", ""), reverse=True)[:20],
            },
            "appeals": {
                "open": sum(item.get("status") == "open" for item in recent_appeals),
                "recent": sorted(recent_appeals, key=lambda item: item.get("created_at", ""), reverse=True)[:10],
            },
        }

    def _increment_stat(self, record_id: str, key: str) -> dict:
        record_id = self._safe_record_id(record_id)
        with self._lock:
            record = self._read_record_locked(record_id)
            stats = record.setdefault("stats", self._empty_stats())
            stats[key] = int(stats.get(key) or 0) + 1
            record["updated_at"] = _utc_now()
            _atomic_json(self.records_dir / record_id / RECORD_FILENAME, record)
            return dict(record)

    def _find_by_fingerprint_locked(self, fingerprint: str) -> Optional[dict]:
        for record in self._read_all_locked():
            if record.get("content_fingerprint") == fingerprint:
                return record
        return None

    def _read_all_locked(self) -> list[dict]:
        if not self.records_dir.exists():
            return []
        records = []
        for path in self.records_dir.glob(f"*/{RECORD_FILENAME}"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(payload, dict):
                records.append(payload)
        return records

    def _read_record_locked(self, record_id: str) -> dict:
        path = self.records_dir / record_id / RECORD_FILENAME
        if not path.is_file():
            raise FileNotFoundError(f"Community record not found: {record_id}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Community record is invalid: {record_id}")
        return payload

    def _read_feedback_locked(self, record_id: str) -> list[dict]:
        path = self.records_dir / record_id / FEEDBACK_FILENAME
        if not path.exists():
            return []
        result = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                result.append(item)
        return result

    def _read_jsonl_locked(self, record_id: str, filename: str) -> list[dict]:
        path = self.records_dir / record_id / filename
        if not path.exists():
            return []
        result = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                result.append(item)
        return result

    def _append_jsonl_locked(self, record_id: str, filename: str, payload: dict) -> None:
        path = self.records_dir / record_id / filename
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _write_jsonl_locked(self, record_id: str, filename: str, items: list[dict]) -> None:
        path = self.records_dir / record_id / filename
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items),
            encoding="utf-8",
        )
        os.replace(temp, path)

    def _append_moderation_audit_locked(self, payload: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / MODERATION_AUDIT_FILENAME
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _read_saved_locked(self) -> dict:
        path = self.root / SAVED_FILENAME
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _saved_workflow_id(saved: dict, record_id: str) -> str:
        entry = saved.get(record_id)
        if isinstance(entry, dict):
            return _clean_text(entry.get("workflow_id"), limit=128)
        if isinstance(entry, str):
            return _clean_text(entry, limit=128)
        return ""

    def _reconcile_feedback_stats_locked(
        self,
        record_id: str,
        record: dict,
        feedback: list[dict],
    ) -> dict:
        expected = self._feedback_stats(record, feedback)
        if record.get("stats") != expected:
            record["stats"] = expected
            record["updated_at"] = _utc_now()
            _atomic_json(self.records_dir / record_id / RECORD_FILENAME, record)
        return record

    @classmethod
    def _feedback_stats(cls, record: dict, feedback: list[dict]) -> dict:
        current = record.get("stats") if isinstance(record.get("stats"), dict) else {}
        stats = cls._empty_stats()
        stats["downloads"] = int(current.get("downloads") or 0)
        stats["imports"] = int(current.get("imports") or 0)
        for item in feedback:
            success = item.get("success")
            if not isinstance(success, bool):
                continue
            stats["feedback_count"] += 1
            stats["success_count" if success else "failure_count"] += 1
            environment = item.get("environment") if isinstance(item.get("environment"), dict) else {}
            matrix_key = " | ".join(
                filter(
                    None,
                    [
                        str(environment.get("os") or ""),
                        str(environment.get("app") or ""),
                        str(environment.get("parser_backend") or ""),
                    ],
                )
            ) or "unspecified"
            bucket = stats["environment_matrix"].setdefault(
                matrix_key,
                {"success": 0, "failure": 0},
            )
            bucket["success" if success else "failure"] += 1
        if stats["feedback_count"]:
            stats["success_rate"] = round(
                stats["success_count"] / stats["feedback_count"],
                4,
            )
        return stats

    @staticmethod
    def _safe_record_id(record_id: str) -> str:
        value = str(record_id or "")
        if not _RECORD_ID_RE.fullmatch(value):
            raise ValueError(f"Invalid record_id: {record_id}")
        return value

    @staticmethod
    def _empty_stats() -> dict:
        return {
            "downloads": 0,
            "imports": 0,
            "feedback_count": 0,
            "success_count": 0,
            "failure_count": 0,
            "success_rate": 0.0,
            "environment_matrix": {},
        }

    @staticmethod
    def _summary(record: dict) -> dict:
        return {
            key: record.get(key)
            for key in (
                "record_id",
                "published_at",
                "updated_at",
                "author",
                "tags",
                "record_license",
                "workflow_id",
                "workflow_name",
                "workflow_title",
                "description",
                "task_description",
                "provenance",
                "environment",
                "understanding",
                "artifacts",
                "recording_verification",
                "reproduction_contract",
                "isolated_reproduction_audit",
                "step_count",
                "platform",
                "stats",
                "publisher_declaration",
                "safety_scan",
                "moderation",
            )
        }

    @staticmethod
    def _clean_publisher_declaration(value: dict | None) -> dict:
        if value is None:
            return {"schema": "gpa.publisher-declaration/v1", "legacy": True}
        if not isinstance(value, dict):
            raise ValueError("publisher_declaration must be an object.")
        required = ("owns_rights", "no_secrets", "safe_content", "public_consent")
        missing = [field for field in required if value.get(field) is not True]
        if missing:
            raise ValueError("Publisher declaration is incomplete: " + ", ".join(missing))
        return {
            "schema": "gpa.publisher-declaration/v1",
            **{field: True for field in required},
            "declared_at": _utc_now(),
        }

    @staticmethod
    def _moderation_payload(record: dict) -> dict:
        value = record.get("moderation") if isinstance(record.get("moderation"), dict) else {}
        status = str(value.get("status") or "published")
        if status not in MODERATION_STATUSES:
            status = "under_review"
        return {
            "status": status,
            "trust_tier": str(value.get("trust_tier") or "automated_checks"),
            "report_count": int(value.get("report_count") or 0),
            "open_report_count": int(value.get("open_report_count") or 0),
            "last_reviewed_at": str(value.get("last_reviewed_at") or ""),
            "reason": str(value.get("reason") or ""),
            **{key: value[key] for key in ("restricted_at",) if key in value},
        }

    @classmethod
    def _moderation_status(cls, record: dict) -> str:
        return cls._moderation_payload(record)["status"]

    @classmethod
    def _ensure_record_available(cls, record: dict, action: str) -> None:
        status = cls._moderation_status(record)
        if status in {"under_review", "restricted", "removed"}:
            raise PermissionError(
                f"Community record is {status.replace('_', ' ')} and cannot be {action}."
            )

    @staticmethod
    def _clean_environment(environment: dict) -> dict:
        if not isinstance(environment, dict):
            raise ValueError("environment must be an object.")
        result = {}
        for key in ("os", "release", "machine", "app", "parser_backend", "screen_size"):
            if key in environment:
                result[key] = _clean_text(environment[key], limit=80)
        return result
