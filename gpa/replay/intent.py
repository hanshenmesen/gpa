"""Offline-first intent parsing for recorded Replays.

The parser deliberately consumes only trusted recording metadata. Screen text and
page content stay outside this boundary and cannot redefine the Replay goal.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

from gpa.replay.domain import ReplayIntent, ReplayStep


ACTION_CAPABILITIES = {
    "click": "pointer",
    "drag": "pointer",
    "scroll": "pointer",
    "type": "keyboard",
    "hotkey": "keyboard",
    "open_url": "navigation",
    "wait": "timing",
    "capture": "screen_capture",
}

CAPABILITY_PERMISSIONS = {
    "pointer": "accessibility",
    "keyboard": "input_control",
    "navigation": "browser_control",
    "screen_capture": "screen_recording",
}

IRREVERSIBLE_WORDS = {
    "send", "submit", "publish", "delete", "purchase", "pay", "transfer",
    "发送", "提交", "发布", "删除", "购买", "付款", "转账",
}


class IntentParser:
    def parse(
        self,
        goal: str,
        steps: Iterable[ReplayStep] = (),
        variable_names: Iterable[str] = (),
    ) -> ReplayIntent:
        normalized_goal = " ".join(str(goal or "").split()).strip()
        step_list = tuple(steps)
        apps = tuple(dict.fromkeys(step.app.strip() for step in step_list if step.app.strip()))
        capabilities = tuple(sorted({
            ACTION_CAPABILITIES.get(step.action_type, step.action_type)
            for step in step_list
            if step.action_type
        }))
        permissions = tuple(sorted({
            CAPABILITY_PERMISSIONS[cap]
            for cap in capabilities
            if cap in CAPABILITY_PERMISSIONS
        }))
        variables = tuple(dict.fromkeys(str(name).strip() for name in variable_names if str(name).strip()))
        objects = self._objects(normalized_goal, step_list, apps)
        irreversible = any(word in normalized_goal.casefold() for word in IRREVERSIBLE_WORDS)
        summary = normalized_goal or self._summary_from_steps(step_list)
        confidence = 1.0 if normalized_goal else (0.75 if step_list else 0.0)
        return ReplayIntent(
            goal=normalized_goal,
            summary=summary,
            apps=apps,
            objects=objects,
            capabilities=capabilities,
            permissions=permissions,
            variables=variables,
            irreversible=irreversible,
            confidence=confidence,
        )

    @staticmethod
    def _summary_from_steps(steps: tuple[ReplayStep, ...]) -> str:
        if not steps:
            return "未提供可解析的 Replay 意图"
        first = steps[0].description.strip() or steps[0].action_type
        return f"执行 {len(steps)} 步工作流：{first}"

    @staticmethod
    def _objects(goal: str, steps: tuple[ReplayStep, ...], apps: tuple[str, ...]) -> tuple[str, ...]:
        text = " ".join([goal, *(step.description for step in steps)])
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9._-]{2,}|[\u4e00-\u9fff]{2,32}", text)
        ignored = {app.casefold() for app in apps} | {
            "click", "type", "press", "open", "scroll", "workflow", "replay",
            "点击", "输入", "打开", "滚动", "工作流", "回放", "执行",
        }
        counts = Counter(token for token in tokens if token.casefold() not in ignored)
        return tuple(token for token, _ in counts.most_common(8))
