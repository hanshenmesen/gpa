"""Persistent isolated runtime Spaces for Replay planning and execution."""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TERMINAL_STATES = {"completed", "failed", "stopped"}
TRANSITIONS = {
    "created": {"planned", "failed"},
    "planned": {"armed", "failed", "stopped"},
    "armed": {"running", "stopped", "failed"},
    "running": TERMINAL_STATES,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReplaySpaceManager:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def create(self, replay_id: str, platform: str) -> dict[str, Any]:
        safe_replay_id = self._safe_id(replay_id, "replay_id")
        space_id = f"space_{uuid.uuid4().hex[:16]}"
        payload = {
            "space_id": space_id,
            "replay_id": safe_replay_id,
            "platform": str(platform),
            "state": "created",
            "created_at": _now(),
            "updated_at": _now(),
            "artifacts": {},
            "error": "",
        }
        with self._lock:
            directory = self.root / space_id
            directory.mkdir(parents=True, exist_ok=False)
            self._write(directory / "space.json", payload)
        return payload

    def attach_plan(self, space_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            space = self.get(space_id)
            self._transition(space, "planned")
            directory = self.root / space["space_id"]
            self._write(directory / "plan.json", plan)
            space["artifacts"]["plan"] = "plan.json"
            self._write(directory / "space.json", space)
            return space

    def get(self, space_id: str) -> dict[str, Any]:
        safe_id = self._safe_id(space_id, "space_id")
        path = self.root / safe_id / "space.json"
        if not path.is_file():
            raise FileNotFoundError(f"Replay Space not found: {safe_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def transition(self, space_id: str, state: str, *, error: str = "") -> dict[str, Any]:
        with self._lock:
            space = self.get(space_id)
            self._transition(space, state)
            if error:
                space["error"] = str(error)[:2000]
            self._write(self.root / space["space_id"] / "space.json", space)
            return space

    @staticmethod
    def _transition(space: dict[str, Any], state: str) -> None:
        current = space["state"]
        if current in TERMINAL_STATES or state not in TRANSITIONS.get(current, set()):
            raise ValueError(f"Invalid Replay Space transition: {current} -> {state}")
        space["state"] = state
        space["updated_at"] = _now()

    @staticmethod
    def _safe_id(value: str, label: str) -> str:
        value = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value):
            raise ValueError(f"Invalid {label}.")
        return value

    @staticmethod
    def _write(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
