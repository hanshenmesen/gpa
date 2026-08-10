"""Portable community packages for GPA workflow records.

The package is a zip file with:
  - gpa_record_manifest.json
  - workflow/workflow.yaml
  - workflow/metadata.json
  - workflow/steps_data.json
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import tempfile
import threading
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

import gpa.storage.workflow as workflow_module
from gpa.storage.workflow import WorkflowStorage, storage as default_storage
from gpa.replay.domain import ReplayStep
from gpa.replay.intent import IntentParser

PACKAGE_FORMAT_VERSION = "1.0"
MANIFEST_NAME = "gpa_record_manifest.json"
WORKFLOW_PREFIX = "workflow/"
REQUIRED_WORKFLOW_FILES = ("workflow.yaml", "metadata.json")
ALLOWED_WORKFLOW_FILES = (*REQUIRED_WORKFLOW_FILES, "steps_data.json")
DEFAULT_MAX_PACKAGE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_MEMBER_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
DEFAULT_MAX_ARCHIVE_MEMBERS = 8
DEFAULT_MAX_MANIFEST_BYTES = 256 * 1024
DEFAULT_MAX_WORKFLOW_YAML_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_METADATA_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_STEPS_DATA_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_GRAPH_NODES_PER_STEP = 2048
DEFAULT_MAX_GRAPH_EDGES_PER_STEP = 16384
DEFAULT_MAX_GRAPH_NODES_PER_PACKAGE = 8192
DEFAULT_MAX_GRAPH_EDGES_PER_PACKAGE = 65536
_IMPORT_LOCK = threading.RLock()


@dataclass
class PackageImportResult:
    workflow_id: str
    workflow_name: str
    storage_dir: Path
    was_renamed: bool = False
    already_saved: bool = False


def export_workflow_package(
    workflow_id: str,
    destination: str | Path,
    *,
    storage: WorkflowStorage = default_storage,
) -> Path:
    """Export a stored workflow as a portable community record package."""
    workflow, _ = storage.load(workflow_id)
    source_dir = workflow.storage_dir
    if not source_dir.exists():
        raise FileNotFoundError(f"Workflow directory not found: {source_dir}")

    destination_path = Path(destination)
    if destination_path.exists() and destination_path.is_dir():
        destination_path = destination_path / _package_filename(workflow.workflow_name, workflow.workflow_id)
    elif destination_path.suffix != ".zip":
        destination_path = destination_path.with_suffix(".gpa-record.zip")
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    file_entries = _collect_workflow_files(source_dir)
    manifest = {
        "format": "gpa-community-record",
        "format_version": PACKAGE_FORMAT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "workflow_id": workflow.workflow_id,
        "workflow_name": workflow.workflow_name,
        "workflow_title": workflow.workflow_title,
        "description": workflow.description,
        "task_description": workflow.task_description,
        "step_count": len(workflow.steps),
        "variable_names": [var.name for var in workflow.variables],
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "privacy": {
            "review_required": True,
            "note": (
                "Workflow packages may include OCR text, typed defaults, app names, "
                "and visual embeddings. Review before sharing publicly."
            ),
        },
        "compatibility": {
            "min_gpa_package_format": PACKAGE_FORMAT_VERSION,
            "requires_step_subgraphs": "steps_data.json" in file_entries,
        },
        "replay": _replay_manifest_metadata(workflow),
        "files": [
            {
                "path": f"{WORKFLOW_PREFIX}{name}",
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
            for name, path in sorted(file_entries.items())
        ],
    }

    with zipfile.ZipFile(destination_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
        for name, path in sorted(file_entries.items()):
            zf.write(path, f"{WORKFLOW_PREFIX}{name}")
    return destination_path


def import_workflow_package(
    package_path: str | Path,
    *,
    workflow_id: Optional[str] = None,
    overwrite: bool = False,
    storage: WorkflowStorage = default_storage,
) -> PackageImportResult:
    """Import a community record package into local workflow storage."""
    package_path = Path(package_path)
    root = workflow_module.WORKFLOWS_DIR
    root.mkdir(parents=True, exist_ok=True)
    with _IMPORT_LOCK:
        staging_root = Path(tempfile.mkdtemp(prefix=".gpa-import-", dir=root))
        snapshot = staging_root / "source.gpa-record.zip"
        backup_dir: Optional[Path] = None
        target_dir: Optional[Path] = None
        try:
            _copy_package_snapshot(package_path, snapshot, max_bytes=DEFAULT_MAX_PACKAGE_BYTES)
            manifest = inspect_workflow_package(snapshot)
            declared_files = {str(item["path"]) for item in manifest["files"]}
            with zipfile.ZipFile(snapshot) as zf:
                extracted = staging_root / "workflow"
                extracted.mkdir()
                _extract_workflow_files(zf, extracted, declared_files)
                _assert_required_files(extracted)
                source_id, workflow_name = _read_workflow_identity(extracted)
                target_id = workflow_id or source_id
                target_dir = _target_workflow_dir(target_id, overwrite)
                was_renamed = target_dir.name != source_id
                _rewrite_workflow_id(extracted, target_dir.name, config_dir=target_dir)

                if target_dir.exists():
                    backup_dir = root / f".gpa-backup-{target_dir.name}-{uuid.uuid4().hex}"
                    os.replace(target_dir, backup_dir)
                try:
                    os.replace(extracted, target_dir)
                except Exception:
                    if backup_dir is not None and backup_dir.exists() and not target_dir.exists():
                        os.replace(backup_dir, target_dir)
                    raise
                try:
                    workflow, _ = storage.load(target_dir.name)
                except Exception:
                    shutil.rmtree(target_dir, ignore_errors=True)
                    if backup_dir is not None and backup_dir.exists():
                        os.replace(backup_dir, target_dir)
                    raise
                if backup_dir is not None:
                    shutil.rmtree(backup_dir, ignore_errors=True)
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)

    return PackageImportResult(
        workflow_id=workflow.workflow_id,
        workflow_name=workflow.workflow_name,
        storage_dir=target_dir,
        was_renamed=was_renamed,
    )


def _copy_package_snapshot(source: Path, destination: Path, *, max_bytes: int) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"Package not found: {source}")
    copied = 0
    with source.open("rb") as src, destination.open("wb") as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            if copied > max_bytes:
                raise ValueError("Package exceeds maximum package size.")
            dst.write(chunk)


def inspect_workflow_package(
    package_path: str | Path,
    *,
    max_package_bytes: int = DEFAULT_MAX_PACKAGE_BYTES,
    max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_archive_members: int = DEFAULT_MAX_ARCHIVE_MEMBERS,
) -> dict:
    """Validate a package without importing it and return its public manifest."""
    package_path = Path(package_path)
    if not package_path.exists():
        raise FileNotFoundError(f"Package not found: {package_path}")
    package_bytes = package_path.stat().st_size
    if package_bytes > max_package_bytes:
        raise ValueError(
            f"Package exceeds maximum package size ({package_bytes} > {max_package_bytes} bytes)."
        )
    try:
        with zipfile.ZipFile(package_path) as zf:
            _validate_archive_limits(
                zf,
                max_member_bytes=max_member_bytes,
                max_uncompressed_bytes=max_uncompressed_bytes,
                max_archive_members=max_archive_members,
            )
            _assert_safe_members(zf)
            manifest = _read_manifest(zf)
            _verify_manifest(manifest, zf)
            _verify_workflow_identity(manifest, zf)
            bad_member = zf.testzip()
            if bad_member:
                raise ValueError(f"Package member failed CRC validation: {bad_member}")
    except zipfile.BadZipFile as exc:
        raise ValueError("Package is not a valid zip archive.") from exc
    return json.loads(json.dumps(manifest))


def _package_filename(workflow_name: str, workflow_id: str) -> str:
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", workflow_name).strip("-") or "workflow"
    return f"{safe_name}-{workflow_id[:8]}.gpa-record.zip"


def _collect_workflow_files(workflow_dir: Path) -> dict[str, Path]:
    files = {
        path.name: path
        for path in workflow_dir.iterdir()
        if path.is_file() and path.name in {"workflow.yaml", "metadata.json", "steps_data.json"}
    }
    missing = [name for name in REQUIRED_WORKFLOW_FILES if name not in files]
    if missing:
        raise FileNotFoundError(f"Workflow is missing required file(s): {', '.join(missing)}")
    return files


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _replay_manifest_metadata(workflow) -> dict:
    steps = tuple(ReplayStep(
        number=step.step_number,
        action_type=step.action_type,
        description=step.action,
        value=step.value,
        app=step.active_app_name,
        pause_seconds=step.pause_duration,
        metadata=dict(step.metadata or {}),
    ) for step in workflow.steps)
    intent = IntentParser().parse(
        workflow.task_description or workflow.description or workflow.workflow_title,
        steps,
        (variable.name for variable in workflow.variables),
    )
    return {
        "schema": "gpa.replay/v1",
        "version": "1.0.0",
        "intent": intent.to_dict(),
        "capabilities": list(intent.capabilities),
        "permissions": list(intent.permissions),
        "platforms": ["darwin", "windows", "linux"],
    }


def _assert_safe_members(zf: zipfile.ZipFile) -> None:
    seen: set[str] = set()
    allowed = {MANIFEST_NAME, *(f"{WORKFLOW_PREFIX}{name}" for name in ALLOWED_WORKFLOW_FILES)}
    for info in zf.infolist():
        name = info.filename
        path = Path(name)
        if (
            not name
            or name in seen
            or name not in allowed
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in name
            or "//" in name
            or any(ord(char) < 32 for char in name)
        ):
            if name in seen:
                raise ValueError(f"Duplicate package member: {name}")
            raise ValueError(f"Unsafe package path: {info.filename}")
        seen.add(name)


def _validate_archive_limits(
    zf: zipfile.ZipFile,
    *,
    max_member_bytes: int,
    max_uncompressed_bytes: int,
    max_archive_members: int,
) -> None:
    members = [info for info in zf.infolist() if not info.is_dir()]
    if len(members) > max_archive_members:
        raise ValueError(f"Package has too many archive members ({len(members)}).")
    total = 0
    path_limits = {
        MANIFEST_NAME: DEFAULT_MAX_MANIFEST_BYTES,
        f"{WORKFLOW_PREFIX}workflow.yaml": DEFAULT_MAX_WORKFLOW_YAML_BYTES,
        f"{WORKFLOW_PREFIX}metadata.json": DEFAULT_MAX_METADATA_BYTES,
        f"{WORKFLOW_PREFIX}steps_data.json": DEFAULT_MAX_STEPS_DATA_BYTES,
    }
    for info in members:
        if info.flag_bits & 0x1:
            raise ValueError(f"Encrypted package member is not supported: {info.filename}")
        effective_limit = min(max_member_bytes, path_limits.get(info.filename, max_member_bytes))
        if info.file_size > effective_limit:
            raise ValueError(f"Package member is too large: {info.filename}")
        total += info.file_size
        if total > max_uncompressed_bytes:
            raise ValueError("Package exceeds maximum uncompressed size.")


def _read_manifest(zf: zipfile.ZipFile) -> dict:
    try:
        raw = zf.read(MANIFEST_NAME)
    except KeyError as exc:
        raise ValueError("Package is missing manifest.") from exc
    if len(raw) > DEFAULT_MAX_MANIFEST_BYTES:
        raise ValueError("Package manifest is too large.")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("Package manifest is not valid JSON.") from exc
    if not isinstance(manifest, dict):
        raise ValueError("Package manifest must be a JSON object.")
    return manifest


def _verify_manifest(manifest: dict, zf: zipfile.ZipFile) -> None:
    if manifest.get("format") != "gpa-community-record":
        raise ValueError("Unsupported package format.")
    if str(manifest.get("format_version")) != PACKAGE_FORMAT_VERSION:
        raise ValueError(f"Unsupported package format version: {manifest.get('format_version')}")
    _validate_manifest_metadata(manifest)
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("Package manifest has no file list.")
    declared_paths: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("Package manifest file entry is invalid.")
        path = str(item.get("path") or "")
        if path in declared_paths:
            raise ValueError(f"Package manifest declares a duplicate file: {path}")
        declared_paths.add(path)
        if path not in {f"{WORKFLOW_PREFIX}{name}" for name in ALLOWED_WORKFLOW_FILES}:
            raise ValueError(f"Unexpected packaged file path: {path}")
        try:
            data = zf.read(path)
        except KeyError as exc:
            raise ValueError(f"Package is missing declared file: {path}") from exc
        if _sha256_bytes(data) != item.get("sha256"):
            raise ValueError(f"Checksum mismatch for packaged file: {path}")
        if _required_nonnegative_int(item.get("bytes"), field="files[].bytes") != len(data):
            raise ValueError(f"Size mismatch for packaged file: {path}")
    required = {f"{WORKFLOW_PREFIX}{name}" for name in REQUIRED_WORKFLOW_FILES}
    missing = sorted(required - declared_paths)
    if missing:
        raise ValueError(f"Package manifest is missing required file(s): {', '.join(missing)}")
    archive_files = {info.filename for info in zf.infolist() if not info.is_dir()}
    undeclared = sorted(archive_files - declared_paths - {MANIFEST_NAME})
    if undeclared:
        raise ValueError(f"Package contains undeclared file(s): {', '.join(undeclared)}")


def _verify_workflow_identity(manifest: dict, zf: zipfile.ZipFile) -> None:
    try:
        workflow = yaml.safe_load(zf.read(f"{WORKFLOW_PREFIX}workflow.yaml")) or {}
        metadata = json.loads(zf.read(f"{WORKFLOW_PREFIX}metadata.json").decode("utf-8"))
    except Exception as exc:
        raise ValueError("Package workflow metadata cannot be parsed.") from exc
    if not isinstance(workflow, dict) or not isinstance(metadata, dict):
        raise ValueError("Package workflow metadata must contain objects.")
    manifest_id = _safe_workflow_id(str(manifest.get("workflow_id") or ""))
    workflow_id = _safe_workflow_id(str(workflow.get("workflow_id") or ""))
    metadata_id = _safe_workflow_id(str(metadata.get("workflow_id") or ""))
    if len({manifest_id, workflow_id, metadata_id}) != 1:
        raise ValueError("Package workflow_id values do not match.")
    for field in ("workflow_name", "workflow_title"):
        if not isinstance(workflow.get(field), str) or not workflow[field].strip():
            raise ValueError(f"Package workflow.yaml is missing a valid {field}.")
        if manifest.get(field) != workflow[field]:
            raise ValueError(f"Package manifest {field} does not match workflow.yaml.")
    steps = workflow.get("steps") or []
    if not isinstance(steps, list):
        raise ValueError("Package workflow steps must be a list.")
    if _required_nonnegative_int(manifest.get("step_count"), field="step_count") != len(steps):
        raise ValueError("Package manifest step_count does not match workflow.yaml.")
    seen_step_numbers: set[int] = set()
    seen_step_ids: set[str] = set()
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("Package workflow steps must contain objects.")
        step_number = step.get("step_number")
        if isinstance(step_number, bool) or not isinstance(step_number, int) or step_number < 1:
            raise ValueError("Package workflow step_number values must be positive integers.")
        action = step.get("Action")
        step_id = step.get("id")
        if not isinstance(action, str) or not action.strip():
            raise ValueError("Package workflow steps must include a non-empty Action.")
        if not isinstance(step_id, str) or not step_id.strip():
            raise ValueError("Package workflow steps must include a non-empty id.")
        if step_number in seen_step_numbers or step_id in seen_step_ids:
            raise ValueError("Package workflow steps must have unique numbers and ids.")
        seen_step_numbers.add(step_number)
        seen_step_ids.add(step_id)
    if f"{WORKFLOW_PREFIX}steps_data.json" in zf.namelist():
        try:
            step_data = json.loads(zf.read(f"{WORKFLOW_PREFIX}steps_data.json").decode("utf-8"))
        except Exception as exc:
            raise ValueError("Package steps_data.json cannot be parsed.") from exc
        if not isinstance(step_data, dict):
            raise ValueError("Package steps_data.json must contain an object.")
        _validate_step_data(step_data, step_ids=seen_step_ids)


def _validate_step_data(step_data: dict, *, step_ids: set[str]) -> None:
    if len(step_data) > len(step_ids) or not set(step_data).issubset(step_ids):
        raise ValueError("Package steps_data.json contains unknown workflow step ids.")
    total_nodes = 0
    total_edges = 0
    for step_id, subgraph in step_data.items():
        if not isinstance(step_id, str) or not isinstance(subgraph, dict):
            raise ValueError("Package steps_data.json entries must be graph objects.")
        ui_graph = subgraph.get("ui_graph")
        graph = ui_graph.get("G") if isinstance(ui_graph, dict) else None
        if not isinstance(graph, dict):
            raise ValueError("Package step graph is missing ui_graph.G.")
        nodes = graph.get("nodes")
        edges = graph.get("edges", graph.get("links"))
        if not isinstance(nodes, list) or not isinstance(edges, list):
            raise ValueError("Package step graph nodes and edges must be lists.")
        if len(nodes) > DEFAULT_MAX_GRAPH_NODES_PER_STEP:
            raise ValueError("Package step graph has too many nodes.")
        if len(edges) > DEFAULT_MAX_GRAPH_EDGES_PER_STEP:
            raise ValueError("Package step graph has too many edges.")
        total_nodes += len(nodes)
        total_edges += len(edges)
        if total_nodes > DEFAULT_MAX_GRAPH_NODES_PER_PACKAGE:
            raise ValueError("Package contains too many graph nodes.")
        if total_edges > DEFAULT_MAX_GRAPH_EDGES_PER_PACKAGE:
            raise ValueError("Package contains too many graph edges.")


def _validate_manifest_metadata(manifest: dict) -> None:
    limits = {
        "workflow_name": 128,
        "workflow_title": 256,
        "description": 2000,
        "task_description": 2000,
    }
    for field, limit in limits.items():
        value = manifest.get(field, "")
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f"Package manifest {field} must be a string of at most {limit} characters.")

    variable_names = manifest.get("variable_names", [])
    if not isinstance(variable_names, list) or len(variable_names) > 100:
        raise ValueError("Package manifest variable_names must be a list with at most 100 entries.")
    if any(not isinstance(value, str) or len(value) > 128 for value in variable_names):
        raise ValueError("Package manifest variable_names entries must be strings of at most 128 characters.")

    for field in ("platform", "compatibility", "privacy"):
        value = manifest.get(field, {})
        if not isinstance(value, dict):
            raise ValueError(f"Package manifest {field} must be an object.")

    replay = manifest.get("replay")
    if replay is not None:
        if not isinstance(replay, dict) or replay.get("schema") != "gpa.replay/v1":
            raise ValueError("Package manifest replay metadata is invalid.")
        intent = replay.get("intent")
        if not isinstance(intent, dict):
            raise ValueError("Package manifest replay intent must be an object.")
        for field in ("goal", "summary"):
            value = intent.get(field, "")
            if not isinstance(value, str) or len(value) > 4000:
                raise ValueError(f"Package manifest replay intent {field} is invalid.")
        for field in ("capabilities", "permissions", "platforms"):
            value = replay.get(field, [])
            if (
                not isinstance(value, list)
                or len(value) > 64
                or any(not isinstance(item, str) or len(item) > 128 for item in value)
            ):
                raise ValueError(f"Package manifest replay {field} is invalid.")


def _required_nonnegative_int(value, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"Package manifest {field} must be a non-negative integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Package manifest {field} must be a non-negative integer.") from exc
    if parsed < 0 or str(parsed) != str(value):
        raise ValueError(f"Package manifest {field} must be a non-negative integer.")
    return parsed


def _extract_workflow_files(
    zf: zipfile.ZipFile,
    destination: Path,
    declared_files: set[str],
) -> None:
    for info in zf.infolist():
        if (
            info.is_dir()
            or info.filename not in declared_files
            or not info.filename.startswith(WORKFLOW_PREFIX)
        ):
            continue
        relative = Path(info.filename.removeprefix(WORKFLOW_PREFIX))
        if not relative.name:
            continue
        out_path = destination / relative
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(out_path, "wb") as dst:
            shutil.copyfileobj(src, dst)


def _assert_required_files(workflow_dir: Path) -> None:
    missing = [name for name in REQUIRED_WORKFLOW_FILES if not (workflow_dir / name).exists()]
    if missing:
        raise ValueError(f"Package workflow is missing required file(s): {', '.join(missing)}")


def _read_workflow_identity(workflow_dir: Path) -> tuple[str, str]:
    with open(workflow_dir / "workflow.yaml") as f:
        data = yaml.safe_load(f) or {}
    workflow_id = str(data.get("workflow_id") or "").strip()
    workflow_name = str(data.get("workflow_name") or "").strip()
    if not workflow_id:
        raise ValueError("Package workflow.yaml is missing workflow_id.")
    return workflow_id, workflow_name


def _target_workflow_dir(workflow_id: str, overwrite: bool) -> Path:
    target_id = _safe_workflow_id(workflow_id)
    target_dir = workflow_module.WORKFLOWS_DIR / target_id
    if overwrite or not target_dir.exists():
        return target_dir
    return workflow_module.WORKFLOWS_DIR / f"{target_id}_{uuid.uuid4().hex[:8]}"


def _safe_workflow_id(workflow_id: str) -> str:
    value = str(workflow_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) or value in {".", ".."}:
        raise ValueError(f"Unsafe workflow_id: {workflow_id}")
    return value


def _rewrite_workflow_id(
    workflow_dir: Path,
    workflow_id: str,
    *,
    config_dir: Optional[Path] = None,
) -> None:
    yaml_path = workflow_dir / "workflow.yaml"
    with open(yaml_path) as f:
        yaml_data = yaml.safe_load(f) or {}
    yaml_data["workflow_id"] = workflow_id
    with open(yaml_path, "w") as f:
        yaml.safe_dump(yaml_data, f, allow_unicode=True, default_flow_style=False)

    metadata_path = workflow_dir / "metadata.json"
    with open(metadata_path) as f:
        meta = json.load(f)
    meta["workflow_id"] = workflow_id
    meta["config_file"] = str((config_dir or workflow_dir) / "workflow.yaml")
    with open(metadata_path, "w") as f:
        json.dump(meta, f, indent=2)
