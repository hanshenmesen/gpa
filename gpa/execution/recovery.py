"""Explicit error-recovery strategies for GUI replay.

Instead of a single generic retry, this module classifies common failure modes
and proposes a targeted, *safe* recovery action, mirroring robustness-oriented
work such as RoTS (arXiv:2605.29447). Safe recovery is enabled by default and
can be disabled with ``GPA_ENABLE_ERROR_RECOVERY=0``. The executor only
auto-applies recoveries flagged ``safe_autofix`` (currently dismissing a
blocking dialog with Esc and waiting for loads); other strategies are advisory.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

RECOVERY_ENABLED_ENV = "GPA_ENABLE_ERROR_RECOVERY"

# Failure-mode signal vocabularies (English + Chinese).
_DIALOG_TERMS = (
    "dialog", "popup", "pop-up", "modal", "alert", "permission", "consent",
    "blocking", "弹窗", "对话框", "允许访问", "权限",
)
_DIALOG_GRAPH_TERMS = (
    "allow", "permission", "cookie", "consent", "accept all", "accept cookies",
    "dismiss", "同意", "允许", "接受",
)
_LOADING_TERMS = (
    "loading", "not ready", "readiness", "still loading", "please wait",
    "spinner", "timeout", "timed out", "加载", "载入", "请稍候",
)
_FOCUS_TERMS = (
    "not active", "frontmost", "focus", "active app", "target app is not active",
    "window", "激活", "焦点", "未在前台",
)
_MOVED_TERMS = (
    "localiz", "not found", "no subgraph", "no match", "moved", "anchor",
    "smc failed", "coordinate", "定位", "未找到", "匹配",
)


@dataclass
class RecoveryStrategy:
    mode: str                 # dismiss_dialog | wait | reactivate_app | reobserve
    reason: str
    action_type: str = ""     # optional immediate action, e.g. "hotkey"
    value: str = ""           # e.g. "esc"
    safe_autofix: bool = False


def recovery_enabled() -> bool:
    value = str(os.environ.get(RECOVERY_ENABLED_ENV, "1") or "").strip().lower()
    return value not in {"0", "false", "no", "n", "off"}


def _graph_text(runtime_graph) -> str:
    if runtime_graph is None:
        return ""
    try:
        return " ".join(str(n.content or "") for n in runtime_graph.nodes).casefold()
    except Exception:
        return ""


def classify_failure(error_text: str, runtime_graph=None) -> Optional[RecoveryStrategy]:
    """Map an error/not-ready signal to a targeted recovery strategy."""
    text = str(error_text or "").casefold()
    graph_text = _graph_text(runtime_graph)

    if any(t in text for t in _DIALOG_TERMS) or any(t in graph_text for t in _DIALOG_GRAPH_TERMS):
        return RecoveryStrategy(
            mode="dismiss_dialog",
            reason="A blocking dialog/popup may be present; dismiss it.",
            action_type="hotkey",
            value="esc",
            safe_autofix=True,
        )
    if any(t in text for t in _LOADING_TERMS):
        return RecoveryStrategy(
            mode="wait",
            reason="The screen may still be loading; wait before retrying.",
            safe_autofix=True,
        )
    if any(t in text for t in _FOCUS_TERMS):
        return RecoveryStrategy(
            mode="reactivate_app",
            reason="Target app/window may not be focused; reactivate it.",
            safe_autofix=False,
        )
    if any(t in text for t in _MOVED_TERMS):
        return RecoveryStrategy(
            mode="reobserve",
            reason="Target may have moved; re-observe the screen and re-localize.",
            safe_autofix=False,
        )
    return None
