"""Dependency-free domain model for portable Replay plugins."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ReplayIntent:
    goal: str
    summary: str
    apps: tuple[str, ...] = ()
    objects: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    variables: tuple[str, ...] = ()
    irreversible: bool = False
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReplayStep:
    number: int
    action_type: str
    description: str
    value: str = ""
    app: str = ""
    pause_seconds: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReplayManifest:
    replay_id: str
    title: str
    description: str
    version: str
    author: str
    source: str
    intent: ReplayIntent
    steps: tuple[ReplayStep, ...]
    variables: tuple[dict[str, str], ...] = ()
    platforms: tuple[str, ...] = ("darwin", "windows", "linux")
    schema: str = "gpa.replay/v1"
    digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlannedStep:
    number: int
    action_type: str
    description: str
    value: str
    app: str
    supported: bool
    degraded: bool = False
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CompatibilityReport:
    platform: str
    status: str
    supported_steps: int
    total_steps: int
    missing_capabilities: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def runnable(self) -> bool:
        return self.status in {"supported", "degraded"} and not self.missing_capabilities

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "runnable": self.runnable}


@dataclass(frozen=True)
class ReplayPlan:
    replay_id: str
    space_id: str
    platform: str
    intent: ReplayIntent
    compatibility: CompatibilityReport
    steps: tuple[PlannedStep, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "replay_id": self.replay_id,
            "space_id": self.space_id,
            "platform": self.platform,
            "intent": self.intent.to_dict(),
            "compatibility": self.compatibility.to_dict(),
            "steps": [step.to_dict() for step in self.steps],
        }
