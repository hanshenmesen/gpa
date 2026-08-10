"""Capability-based cross-system Replay planning."""
from __future__ import annotations

import platform as host_platform

from gpa.replay.domain import (
    CompatibilityReport,
    PlannedStep,
    ReplayManifest,
)


PLATFORM_NAMES = {
    "darwin": "darwin",
    "macos": "darwin",
    "windows": "windows",
    "linux": "linux",
}

APP_ALIASES = {
    "darwin": {
        "notepad": "TextEdit",
        "file explorer": "Finder",
        "microsoft edge": "Microsoft Edge",
    },
    "windows": {
        "textedit": "Notepad",
        "finder": "File Explorer",
        "safari": "Microsoft Edge",
    },
    "linux": {
        "textedit": "Text Editor",
        "finder": "Files",
        "safari": "Firefox",
    },
}

SUPPORTED_ACTIONS = {
    "darwin": {"click", "drag", "scroll", "type", "hotkey", "open_url", "wait", "capture"},
    "windows": {"click", "drag", "scroll", "type", "hotkey", "open_url", "wait", "capture"},
    "linux": {"click", "drag", "scroll", "type", "hotkey", "open_url", "wait", "capture"},
}


def current_platform() -> str:
    return PLATFORM_NAMES.get(host_platform.system().casefold(), host_platform.system().casefold())


class PlatformPlanner:
    def plan_steps(self, manifest: ReplayManifest, platform: str) -> tuple[tuple[PlannedStep, ...], CompatibilityReport]:
        target = PLATFORM_NAMES.get(str(platform).casefold(), str(platform).casefold())
        supported_actions = SUPPORTED_ACTIONS.get(target, set())
        planned: list[PlannedStep] = []
        warnings: list[str] = []
        missing: set[str] = set()

        if target not in manifest.platforms:
            missing.add(f"platform:{target}")

        for step in manifest.steps:
            supported = step.action_type in supported_actions
            notes: list[str] = []
            app = APP_ALIASES.get(target, {}).get(step.app.casefold(), step.app)
            degraded = bool(step.app and app != step.app)
            value = self._map_hotkey(step.action_type, step.value, target)
            if degraded:
                notes.append(f"应用映射：{step.app} -> {app}")
            if value != step.value:
                degraded = True
                notes.append(f"快捷键映射：{step.value} -> {value}")
            if step.metadata.get("coordinate_only") and target != "darwin":
                supported = False
                notes.append("仅坐标目标不能跨系统安全重放")
                missing.add("semantic_target")
            if not supported:
                missing.add(f"action:{step.action_type}")
            planned.append(PlannedStep(
                number=step.number,
                action_type=step.action_type,
                description=step.description,
                value=value,
                app=app,
                supported=supported,
                degraded=degraded,
                notes=tuple(notes),
            ))
            warnings.extend(notes)

        supported_count = sum(1 for step in planned if step.supported)
        status = "unsupported" if missing else ("degraded" if warnings else "supported")
        report = CompatibilityReport(
            platform=target,
            status=status,
            supported_steps=supported_count,
            total_steps=len(planned),
            missing_capabilities=tuple(sorted(missing)),
            warnings=tuple(warnings),
        )
        return tuple(planned), report

    @staticmethod
    def _map_hotkey(action_type: str, value: str, platform: str) -> str:
        if action_type != "hotkey":
            return value
        if platform == "darwin":
            return value.replace("ctrl+", "cmd+") if value.startswith("ctrl+") else value
        return value.replace("cmd+", "ctrl+") if value.startswith("cmd+") else value
