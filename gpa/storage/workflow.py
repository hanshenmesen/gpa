"""File-backed workflow domain objects and persistence.

The on-disk layout intentionally remains compatible with the original GPA
paper artifacts and the community package format::

    storage/workflows/<workflow_id>/
        workflow.yaml
        metadata.json
        steps_data.json  # optional visual replay context
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from gpa.config import WORKFLOWS_DIR as CONFIG_WORKFLOWS_DIR
from gpa.core.ui_graph import StepSubgraph

# Kept mutable for the local web server and isolated tests, which redirect the
# repository at runtime.
WORKFLOWS_DIR = CONFIG_WORKFLOWS_DIR
_WORKFLOW_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_workflow_id(value: str) -> str:
    workflow_id = str(value or "").strip()
    if (
        workflow_id in {"", ".", ".."}
        or _WORKFLOW_ID_RE.fullmatch(workflow_id) is None
    ):
        raise ValueError(f"Unsafe workflow_id: {value}")
    return workflow_id


def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class WorkflowVariable:
    name: str
    default_value: str = ""
    description: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "default_value": self.default_value,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WorkflowVariable":
        return cls(
            name=_as_text(raw.get("name")).strip(),
            default_value=_as_text(raw.get("default_value")),
            description=_as_text(raw.get("description")),
        )


@dataclass
class WorkflowStep:
    step_number: int
    action: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    action_type: str = "click"
    value: str = ""
    pause_duration: float = 0.5
    active_app_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_number": self.step_number,
            # Preserve the paper-compatible capitalized field name.
            "Action": self.action,
            "id": self.id,
            "action_type": self.action_type,
            "value": self.value,
            "pause_duration": self.pause_duration,
            "active_app_name": self.active_app_name,
            "metadata": dict(self.metadata or {}),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], *, fallback_number: int) -> "WorkflowStep":
        raw_number = raw.get("step_number", fallback_number)
        try:
            number = int(raw_number)
        except (TypeError, ValueError):
            number = fallback_number
        step_id = _as_text(raw.get("id")).strip() or str(uuid.uuid4())
        action = _as_text(raw.get("Action", raw.get("action", f"Step {number}")))
        metadata = raw.get("metadata")
        return cls(
            step_number=number,
            action=action,
            id=step_id,
            action_type=_as_text(raw.get("action_type"), "click").strip() or "click",
            value=_as_text(raw.get("value")),
            pause_duration=_as_float(raw.get("pause_duration"), 0.5),
            active_app_name=_as_text(raw.get("active_app_name")),
            metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
        )


@dataclass
class Workflow:
    workflow_id: str
    workflow_name: str
    workflow_title: str
    description: str
    variables: list[WorkflowVariable] = field(default_factory=list)
    steps: list[WorkflowStep] = field(default_factory=list)
    task_description: str = ""
    category: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)
    environment: dict[str, Any] = field(default_factory=dict)
    understanding: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_now)
    _storage_dir: Path | None = field(default=None, repr=False, compare=False)

    @property
    def storage_dir(self) -> Path:
        return self._storage_dir or (WORKFLOWS_DIR / _safe_workflow_id(self.workflow_id))

    def to_yaml_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "workflow_name": self.workflow_name,
            "workflow_title": self.workflow_title,
            "description": self.description,
            "task_description": self.task_description,
            "provenance": dict(self.provenance or {}),
            "environment": dict(self.environment or {}),
            "understanding": dict(self.understanding or {}),
            "artifacts": dict(self.artifacts or {}),
            "running_config": {
                "variable_values": {
                    variable.name: variable.default_value for variable in self.variables
                },
                "category": self.category,
            },
            "steps": [step.to_dict() for step in self.steps],
        }


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_json(path: Path, *, default: Any) -> Any:
    if not path.is_file():
        return default
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


class WorkflowStorage:
    """Persist and retrieve workflows from the configured local repository."""

    def __init__(self, workflows_dir: str | Path | None = None) -> None:
        self._workflows_dir = Path(workflows_dir).resolve() if workflows_dir is not None else None

    @property
    def workflows_dir(self) -> Path:
        # Keep the default storage dynamically bound to the module-level path
        # so existing server/test isolation remains compatible.
        return self._workflows_dir or WORKFLOWS_DIR

    def save(
        self,
        workflow: Workflow,
        step_subgraphs: Mapping[str, StepSubgraph] | None = None,
    ) -> Path:
        workflow_id = _safe_workflow_id(workflow.workflow_id)
        workflow.workflow_id = workflow_id
        workflow_dir = self.workflows_dir / workflow_id
        workflow_dir.mkdir(parents=True, exist_ok=True)
        workflow._storage_dir = workflow_dir

        yaml_payload = yaml.safe_dump(
            workflow.to_yaml_dict(),
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        metadata = {
            "workflow_id": workflow_id,
            "created_at": workflow.created_at or _utc_now(),
            "config_file": str(workflow_dir / "workflow.yaml"),
            "workflow_metadata": {
                "variables": [variable.to_dict() for variable in workflow.variables],
                "task_description": workflow.task_description,
                "category": workflow.category,
                "provenance": dict(workflow.provenance or {}),
                "environment": dict(workflow.environment or {}),
                "understanding": dict(workflow.understanding or {}),
                "artifacts": dict(workflow.artifacts or {}),
            },
        }
        graph_payload = {
            str(step_id): subgraph.to_dict()
            for step_id, subgraph in (step_subgraphs or {}).items()
        }

        _atomic_write_text(workflow_dir / "workflow.yaml", yaml_payload)
        _atomic_write_text(
            workflow_dir / "metadata.json",
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        )
        # Keep an explicit empty object for workflows without visual context.
        # Community packages rely on this stable member being present so they
        # can be validated and upgraded without changing archive topology.
        _atomic_write_text(
            workflow_dir / "steps_data.json",
            json.dumps(graph_payload, ensure_ascii=False, indent=2) + "\n",
        )
        _atomic_write_text(
            workflow_dir / "environment.json",
            json.dumps(workflow.environment or {}, ensure_ascii=False, indent=2) + "\n",
        )
        _atomic_write_text(
            workflow_dir / "understanding.json",
            json.dumps(workflow.understanding or {}, ensure_ascii=False, indent=2) + "\n",
        )
        return workflow_dir

    def load(self, workflow_id: str) -> tuple[Workflow, dict[str, StepSubgraph]]:
        safe_id = _safe_workflow_id(workflow_id)
        workflow_dir = self.workflows_dir / safe_id
        yaml_path = workflow_dir / "workflow.yaml"
        if not yaml_path.is_file():
            raise FileNotFoundError(f"Workflow not found: {safe_id}")

        with yaml_path.open(encoding="utf-8") as stream:
            raw = yaml.safe_load(stream) or {}
        if not isinstance(raw, Mapping):
            raise ValueError(f"Invalid workflow.yaml for {safe_id}")

        metadata = _load_json(workflow_dir / "metadata.json", default={})
        if not isinstance(metadata, Mapping):
            raise ValueError(f"Invalid metadata.json for {safe_id}")
        workflow_metadata = metadata.get("workflow_metadata")
        if not isinstance(workflow_metadata, Mapping):
            workflow_metadata = {}
        running_config = raw.get("running_config")
        if not isinstance(running_config, Mapping):
            running_config = {}
        environment_sidecar = _load_json(workflow_dir / "environment.json", default={})
        understanding_sidecar = _load_json(workflow_dir / "understanding.json", default={})

        variables_raw = workflow_metadata.get("variables")
        if not isinstance(variables_raw, list):
            variables_raw = raw.get("variables") if isinstance(raw.get("variables"), list) else []
        variables = [
            WorkflowVariable.from_dict(item)
            for item in variables_raw
            if isinstance(item, Mapping) and _as_text(item.get("name")).strip()
        ]
        if not variables:
            variable_values = running_config.get("variable_values")
            if isinstance(variable_values, Mapping):
                variables = [
                    WorkflowVariable(str(name), _as_text(value))
                    for name, value in variable_values.items()
                ]

        steps_raw = raw.get("steps")
        if not isinstance(steps_raw, list):
            raise ValueError(f"Workflow steps must be a list: {safe_id}")
        steps = [
            WorkflowStep.from_dict(item, fallback_number=index)
            for index, item in enumerate(steps_raw, 1)
            if isinstance(item, Mapping)
        ]

        persisted_id = _safe_workflow_id(
            _as_text(raw.get("workflow_id"), safe_id).strip() or safe_id
        )
        if persisted_id != safe_id:
            raise ValueError(
                f"Workflow identity mismatch: directory {safe_id!r}, file {persisted_id!r}"
            )

        workflow = Workflow(
            workflow_id=persisted_id,
            workflow_name=_as_text(raw.get("workflow_name"), safe_id).strip() or safe_id,
            workflow_title=_as_text(raw.get("workflow_title"), safe_id).strip() or safe_id,
            description=_as_text(raw.get("description")),
            variables=variables,
            steps=steps,
            task_description=_as_text(
                raw.get("task_description", workflow_metadata.get("task_description", ""))
            ),
            category=_as_text(
                running_config.get("category", workflow_metadata.get("category", ""))
            ),
            provenance=dict(
                raw.get("provenance")
                if isinstance(raw.get("provenance"), Mapping)
                else workflow_metadata.get("provenance")
                if isinstance(workflow_metadata.get("provenance"), Mapping)
                else {}
            ),
            environment=dict(
                environment_sidecar
                if isinstance(environment_sidecar, Mapping) and environment_sidecar
                else raw.get("environment")
                if isinstance(raw.get("environment"), Mapping)
                else workflow_metadata.get("environment")
                if isinstance(workflow_metadata.get("environment"), Mapping)
                else {}
            ),
            understanding=dict(
                understanding_sidecar
                if isinstance(understanding_sidecar, Mapping) and understanding_sidecar
                else raw.get("understanding")
                if isinstance(raw.get("understanding"), Mapping)
                else workflow_metadata.get("understanding")
                if isinstance(workflow_metadata.get("understanding"), Mapping)
                else {}
            ),
            artifacts=dict(
                raw.get("artifacts")
                if isinstance(raw.get("artifacts"), Mapping)
                else workflow_metadata.get("artifacts")
                if isinstance(workflow_metadata.get("artifacts"), Mapping)
                else {}
            ),
            created_at=_as_text(metadata.get("created_at"), _utc_now()),
            _storage_dir=workflow_dir,
        )

        graph_data = _load_json(workflow_dir / "steps_data.json", default={})
        if not isinstance(graph_data, Mapping):
            raise ValueError(f"Invalid steps_data.json for {safe_id}")
        subgraphs = {
            str(step_id): StepSubgraph.from_dict(item)
            for step_id, item in graph_data.items()
            if isinstance(item, Mapping)
        }
        return workflow, subgraphs

    def list_workflows(self) -> list[dict[str, Any]]:
        if not self.workflows_dir.exists():
            return []
        rows: list[dict[str, Any]] = []
        for workflow_dir in sorted(self.workflows_dir.iterdir(), key=lambda path: path.name):
            if not workflow_dir.is_dir() or workflow_dir.name.startswith("."):
                continue
            try:
                workflow, _ = self.load(workflow_dir.name)
            except (FileNotFoundError, ValueError, KeyError, TypeError, json.JSONDecodeError, yaml.YAMLError):
                continue
            rows.append({
                "id": workflow.workflow_id,
                "name": workflow.workflow_name,
                "title": workflow.workflow_title,
                "description": workflow.description,
                "task_description": workflow.task_description,
                "category": workflow.category,
                "created_at": workflow.created_at,
                "steps": len(workflow.steps),
                "variables": len(workflow.variables),
            })
        rows.sort(key=lambda item: item["id"])
        return rows

    def delete(self, workflow_id: str) -> None:
        safe_id = _safe_workflow_id(workflow_id)
        workflow_dir = self.workflows_dir / safe_id
        if not workflow_dir.exists():
            raise FileNotFoundError(f"Workflow not found: {safe_id}")
        shutil.rmtree(workflow_dir)


storage = WorkflowStorage()


__all__ = [
    "Workflow",
    "WorkflowStep",
    "WorkflowStorage",
    "WorkflowVariable",
    "WORKFLOWS_DIR",
    "storage",
]
