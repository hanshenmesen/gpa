"""Standardized benchmark adapters and a grounding evaluation harness.

This provides a thin, dependency-light bridge between GPA and external GUI-agent
benchmarks:

  * A ScreenSpot-style *grounding* harness that scores the configured grounding
    backend (see ``gpa.core.grounding``) by checking whether its predicted click
    point falls inside each sample's target box.
  * A pluggable *adapter* registry for interactive benchmarks such as macOSWorld
    (arXiv:2506.04135) and WindowsWorld (arXiv:2604.27776), whose environments
    live outside this repo; callers register an adapter that yields tasks.

The harness and structures are fully testable with in-memory samples; concrete
dataset loaders are supplied by the caller.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from gpa.core.grounding import GroundingRequest, run_grounder

# ──────────────────────────────────────────────────────────────────────────── #
# Grounding evaluation (ScreenSpot / ScreenSpot-Pro style)                      #
# ──────────────────────────────────────────────────────────────────────────── #

@dataclass
class GroundingSample:
    instruction: str
    target_box: tuple[float, float, float, float]  # (x, y, w, h) in screen px
    screenshot: object = None
    live_size: Optional[tuple[int, int]] = None
    sample_id: str = ""


@dataclass
class GroundingEvalResult:
    total: int = 0
    hits: int = 0
    misses: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return (self.hits / self.total) if self.total else 0.0

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "hits": self.hits,
            "accuracy": round(self.accuracy, 4),
            "misses": list(self.misses),
        }


def point_in_box(x: float, y: float, box: tuple[float, float, float, float]) -> bool:
    bx, by, bw, bh = box
    return bx <= x <= bx + bw and by <= y <= by + bh


def evaluate_grounding(
    samples: Iterable[GroundingSample],
    *,
    backend: Optional[str] = None,
) -> GroundingEvalResult:
    """Score the grounding backend on ScreenSpot-style samples.

    A sample counts as a hit when the backend's predicted point lies inside the
    target box. Samples the backend cannot localize count as misses.
    """
    result = GroundingEvalResult()
    for sample in samples:
        result.total += 1
        prediction = run_grounder(
            GroundingRequest(
                instruction=sample.instruction,
                screenshot=sample.screenshot,
                live_size=sample.live_size,
            ),
            name=backend,
        )
        sid = sample.sample_id or sample.instruction[:40]
        if prediction is None:
            result.misses.append(sid)
            continue
        if point_in_box(prediction.x, prediction.y, sample.target_box):
            result.hits += 1
        else:
            result.misses.append(sid)
    return result


# ──────────────────────────────────────────────────────────────────────────── #
# Interactive benchmark adapters (macOSWorld / WindowsWorld / OSWorld …)        #
# ──────────────────────────────────────────────────────────────────────────── #

@dataclass
class BenchmarkTask:
    task_id: str
    instruction: str
    app: str = ""
    platform: str = ""
    language: str = "en"
    metadata: dict = field(default_factory=dict)


BenchmarkAdapter = Callable[[], list[BenchmarkTask]]

_adapters: dict[str, BenchmarkAdapter] = {}


def register_benchmark_adapter(name: str, adapter: BenchmarkAdapter) -> None:
    key = str(name or "").strip().casefold()
    if not key:
        raise ValueError("Benchmark adapter name cannot be empty.")
    if not callable(adapter):
        raise TypeError("adapter must be callable.")
    _adapters[key] = adapter


def unregister_benchmark_adapter(name: str) -> None:
    _adapters.pop(str(name or "").strip().casefold(), None)


def list_benchmark_adapters() -> list[str]:
    return sorted(_adapters)


def load_benchmark_tasks(name: str) -> list[BenchmarkTask]:
    key = str(name or "").strip().casefold()
    adapter = _adapters.get(key)
    if adapter is None:
        raise ValueError(
            f"Unknown benchmark adapter: {name}. "
            f"Registered: {', '.join(list_benchmark_adapters()) or '(none)'}."
        )
    tasks = adapter() or []
    return list(tasks)
