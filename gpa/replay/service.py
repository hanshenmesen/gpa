"""Application service joining legacy workflows to the canonical Replay model."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from gpa.replay.domain import ReplayManifest, ReplayPlan, ReplayStep
from gpa.replay.intent import IntentParser
from gpa.replay.platforms import PlatformPlanner, current_platform
from gpa.replay.spaces import ReplaySpaceManager


class ReplayService:
    def __init__(self, workflow_storage, *, spaces_root: Path):
        self.workflow_storage = workflow_storage
        self.intent_parser = IntentParser()
        self.platform_planner = PlatformPlanner()
        self.spaces = ReplaySpaceManager(spaces_root)

    def list_replays(self, *, platform: str | None = None) -> list[dict[str, Any]]:
        target = platform or current_platform()
        replays = []
        for summary in self.workflow_storage.list_workflows():
            try:
                manifest = self.get_replay(summary["id"])
                _, compatibility = self.platform_planner.plan_steps(manifest, target)
                replays.append({
                    "replay_id": manifest.replay_id,
                    "title": manifest.title,
                    "description": manifest.description,
                    "version": manifest.version,
                    "source": manifest.source,
                    "step_count": len(manifest.steps),
                    "intent": manifest.intent.to_dict(),
                    "compatibility": compatibility.to_dict(),
                    "digest": manifest.digest,
                })
            except (FileNotFoundError, ValueError, KeyError):
                continue
        return replays

    def get_replay(self, replay_id: str) -> ReplayManifest:
        workflow, subgraphs = self.workflow_storage.load(replay_id)
        steps = tuple(ReplayStep(
            number=step.step_number,
            action_type=step.action_type,
            description=step.action,
            value=step.value,
            app=step.active_app_name,
            pause_seconds=step.pause_duration,
            metadata={
                **dict(step.metadata or {}),
                **({"coordinate_only": True} if (
                    step.action_type in {"click", "drag", "scroll"} and step.id not in subgraphs
                ) else {}),
            },
        ) for step in workflow.steps)
        variables = tuple({
            "name": variable.name,
            "default_value": variable.default_value,
            "description": variable.description,
        } for variable in workflow.variables)
        intent = self.intent_parser.parse(
            workflow.task_description or workflow.description or workflow.workflow_title,
            steps,
            (variable.name for variable in workflow.variables),
        )
        manifest = ReplayManifest(
            replay_id=workflow.workflow_id,
            title=workflow.workflow_title or workflow.workflow_name,
            description=workflow.description,
            version="1.0.0",
            author="local",
            source="local-recording",
            intent=intent,
            steps=steps,
            variables=variables,
        )
        canonical = json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return replace(manifest, digest=f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}")

    def parse_intent(self, goal: str, raw_steps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        steps = tuple(self._step_from_dict(index, step) for index, step in enumerate(raw_steps or [], 1))
        return self.intent_parser.parse(goal, steps).to_dict()

    def plan(self, replay_id: str, *, platform: str | None = None) -> ReplayPlan:
        target = platform or current_platform()
        manifest = self.get_replay(replay_id)
        planned_steps, compatibility = self.platform_planner.plan_steps(manifest, target)
        space = self.spaces.create(replay_id, target)
        plan = ReplayPlan(
            replay_id=replay_id,
            space_id=space["space_id"],
            platform=target,
            intent=manifest.intent,
            compatibility=compatibility,
            steps=planned_steps,
        )
        self.spaces.attach_plan(space["space_id"], plan.to_dict())
        return plan

    @staticmethod
    def _step_from_dict(index: int, raw: dict[str, Any]) -> ReplayStep:
        if not isinstance(raw, dict):
            raise ValueError(f"Step {index} must be an object.")
        return ReplayStep(
            number=int(raw.get("number") or raw.get("step_number") or index),
            action_type=str(raw.get("action_type") or "click"),
            description=str(raw.get("description") or raw.get("action") or ""),
            value=str(raw.get("value") or ""),
            app=str(raw.get("app") or raw.get("active_app_name") or ""),
            pause_seconds=float(raw.get("pause_seconds") or raw.get("pause_duration") or 0.5),
            metadata=dict(raw.get("metadata") or {}),
        )
