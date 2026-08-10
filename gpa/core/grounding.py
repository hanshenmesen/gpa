"""Pluggable visual grounding backends for GUI target localization.

A *grounder* maps a natural-language instruction (step description / target
hint) plus the current screenshot to on-screen click coordinates. This mirrors
recent vision-grounding models such as UGround (arXiv:2410.05243) and GTA1
(arXiv:2507.05791), which localize elements directly from pixels instead of
relying on OCR text or recorded coordinates.

This module only defines the interface, a registry, and a localization adapter.
Concrete model backends are *registered by the caller* so the core package stays
dependency-light and fully testable. Grounding is disabled by default and only
activates when ``GPA_GROUNDING_BACKEND`` names a registered backend.

Typical usage::

    from gpa.core.grounding import register_grounder, GroundingResult

    def my_uground_backend(request):
        x, y = my_model.predict(request.instruction, request.screenshot)
        return GroundingResult(x=x, y=y, confidence=0.9)

    register_grounder("uground", my_uground_backend)
    # then run with GPA_GROUNDING_BACKEND=uground
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

GROUNDING_BACKEND_ENV = "GPA_GROUNDING_BACKEND"
GROUNDING_MIN_CONF_ENV = "GPA_GROUNDING_MIN_CONF"
DEFAULT_GROUNDING_MIN_CONF = 0.6


@dataclass
class GroundingRequest:
    """Everything a grounder needs to localize a target on the live screen."""

    instruction: str
    screenshot: object = None                    # PIL.Image or None
    live_size: Optional[tuple[int, int]] = None  # (width, height) of screenshot
    runtime_graph: object = None                 # parsed UIGraph, optional hint
    action_type: str = ""


@dataclass
class GroundingResult:
    """A localized target from a grounding backend."""

    x: float
    y: float
    confidence: float
    method: str = "grounder"
    # "screen_pixels" (default) or "normalized" (0..1, scaled by live_size).
    coordinate_space: str = "screen_pixels"


Grounder = Callable[[GroundingRequest], Optional[GroundingResult]]

_grounders: dict[str, Grounder] = {}


def register_grounder(name: str, grounder: Grounder) -> None:
    """Register a grounding backend under ``name`` (case-insensitive)."""
    key = str(name or "").strip().casefold()
    if not key:
        raise ValueError("Grounder name cannot be empty.")
    if key == "none":
        raise ValueError("'none' is reserved to disable grounding.")
    if not callable(grounder):
        raise TypeError("grounder must be callable.")
    _grounders[key] = grounder


def unregister_grounder(name: str) -> None:
    _grounders.pop(str(name or "").strip().casefold(), None)


def list_grounders() -> list[str]:
    return sorted(_grounders)


def _configured_backend() -> str:
    return str(os.environ.get(GROUNDING_BACKEND_ENV, "none") or "none").strip().casefold()


def grounding_enabled() -> bool:
    """True only when a registered backend is selected via the env var."""
    backend = _configured_backend()
    return backend not in {"", "none"} and backend in _grounders


def grounding_min_conf() -> float:
    raw = os.environ.get(GROUNDING_MIN_CONF_ENV)
    if raw is None:
        return DEFAULT_GROUNDING_MIN_CONF
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return DEFAULT_GROUNDING_MIN_CONF


def get_grounder(name: Optional[str] = None) -> Optional[Grounder]:
    key = str(name or _configured_backend() or "none").strip().casefold()
    if key in {"", "none"}:
        return None
    return _grounders.get(key)


def _resolve_coordinates(result: GroundingResult, live_size: Optional[tuple[int, int]]) -> Optional[GroundingResult]:
    space = str(result.coordinate_space or "screen_pixels").strip().casefold()
    if space == "normalized":
        if not live_size:
            return None
        return GroundingResult(
            x=float(result.x) * float(live_size[0]),
            y=float(result.y) * float(live_size[1]),
            confidence=float(result.confidence),
            method=result.method,
            coordinate_space="screen_pixels",
        )
    return result


def run_grounder(
    request: GroundingRequest,
    *,
    name: Optional[str] = None,
) -> Optional[GroundingResult]:
    """Run the selected grounder, returning screen-pixel coordinates or None.

    Never raises: a misbehaving backend degrades gracefully to None so the
    executor can fall back to SMC / recorded coordinates.
    """
    grounder = get_grounder(name)
    if grounder is None:
        return None
    if not str(request.instruction or "").strip():
        return None
    try:
        result = grounder(request)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Grounder %r failed: %s", name or _configured_backend(), exc, exc_info=True)
        return None
    if result is None:
        return None
    live_size = request.live_size
    if live_size is None and request.screenshot is not None:
        try:
            live_size = (int(request.screenshot.width), int(request.screenshot.height))
        except Exception:
            live_size = None
    resolved = _resolve_coordinates(result, live_size)
    if resolved is None:
        return None
    try:
        resolved.confidence = max(0.0, min(1.0, float(resolved.confidence)))
    except (TypeError, ValueError):
        return None
    return resolved
