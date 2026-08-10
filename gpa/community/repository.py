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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from gpa.community.package import (
    DEFAULT_MAX_PACKAGE_BYTES,
    PackageImportResult,
    import_workflow_package,
    inspect_workflow_package,
)
from gpa.storage.workflow import WorkflowStorage


RECORD_SCHEMA_VERSION = "1.0"
PACKAGE_FILENAME = "package.gpa-record.zip"
RECORD_FILENAME = "record.json"
FEEDBACK_FILENAME = "feedback.jsonl"
SAVED_FILENAME = "saved_records.json"
ALLOWED_LICENSES = {"CC-BY-4.0", "CC0-1.0", "MIT", "Apache-2.0"}
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
        package: str | Path | bytes,
        *,
        author: str,
        tags: list[str],
        license_id: str,
        privacy_reviewed: bool,
    ) -> dict:
        if privacy_reviewed is not True:
            raise ValueError("Explicit privacy review is required before publishing.")
        author = _clean_text(author, limit=80, fallback="Anonymous")
        tags = _clean_tags(tags)
        license_id = str(license_id or "").strip()
        if license_id not in ALLOWED_LICENSES:
            raise ValueError(f"Unsupported record license: {license_id}")

        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".community-publish-", dir=self.root))
        staged_package = staging / PACKAGE_FILENAME
        try:
            if isinstance(package, bytes):
                if len(package) > self.max_package_bytes:
                    raise ValueError("Package exceeds maximum package size.")
                staged_package.write_bytes(package)
            else:
                source = Path(package)
                if not source.exists():
                    raise FileNotFoundError(f"Package not found: {source}")
                if source.stat().st_size > self.max_package_bytes:
                    raise ValueError("Package exceeds maximum package size.")
                shutil.copyfile(source, staged_package)

            manifest = inspect_workflow_package(
                staged_package,
                max_package_bytes=self.max_package_bytes,
            )
            fingerprint = _content_fingerprint(manifest)
            package_sha256 = _sha256(staged_package)
            now = _utc_now()

            with self._lock:
                self.records_dir.mkdir(parents=True, exist_ok=True)
                existing = self._find_by_fingerprint_locked(fingerprint)
                if existing is not None:
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
                    "content_fingerprint": fingerprint,
                    "package_sha256": package_sha256,
                    "package_bytes": (staged_record_dir / PACKAGE_FILENAME).stat().st_size,
                    "workflow_id": str(manifest.get("workflow_id") or ""),
                    "workflow_name": str(manifest.get("workflow_name") or ""),
                    "workflow_title": str(manifest.get("workflow_title") or ""),
                    "description": str(manifest.get("description") or ""),
                    "task_description": str(manifest.get("task_description") or ""),
                    "step_count": int(manifest.get("step_count") or 0),
                    "variable_names": list(manifest.get("variable_names") or []),
                    "platform": dict(manifest.get("platform") or {}),
                    "compatibility": dict(manifest.get("compatibility") or {}),
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
            haystack = " ".join(
                [
                    record.get("record_id", ""),
                    record.get("workflow_name", ""),
                    record.get("workflow_title", ""),
                    record.get("description", ""),
                    record.get("task_description", ""),
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
        return sorted(filtered, key=lambda item: item.get("published_at", ""), reverse=True)

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

    def import_record(
        self,
        record_id: str,
        *,
        workflow_id: Optional[str] = None,
        storage: WorkflowStorage,
    ) -> PackageImportResult:
        record_id = self._safe_record_id(record_id)
        with self._lock:
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
            result = import_workflow_package(
                path,
                workflow_id=workflow_id,
                overwrite=False,
                storage=storage,
            )
            saved[record_id] = {
                "workflow_id": result.workflow_id,
                "saved_at": _utc_now(),
            }
            try:
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
                "step_count",
                "platform",
                "stats",
            )
        }

    @staticmethod
    def _clean_environment(environment: dict) -> dict:
        if not isinstance(environment, dict):
            raise ValueError("environment must be an object.")
        result = {}
        for key in ("os", "release", "machine", "app", "parser_backend", "screen_size"):
            if key in environment:
                result[key] = _clean_text(environment[key], limit=80)
        return result
