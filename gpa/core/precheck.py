"""Readiness checker and precheck pipeline.

Combines SMC localization confidence into a readiness gate before
each action. Wraps the precheck background-thread pipeline from
Appendix C of the paper.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Optional

from gpa.config import (
    PRECHECK_LOOKAHEAD,
    PRECHECK_MIN_CONF,
    READINESS_THRESHOLD,
)
from gpa.core.smc import LocalizationResult, localize
from gpa.core.ui_graph import StepSubgraph, UIGraph

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────── #
# Readiness check                                                              #
# ──────────────────────────────────────────────────────────────────────────── #

@dataclass
class ReadinessResult:
    ready: bool
    result: Optional[LocalizationResult]
    confidence: float
    step_idx: int


def check_readiness(
    subgraph: StepSubgraph,
    runtime_graph: UIGraph,
    live_size: tuple[int, int],
    threshold: float = READINESS_THRESHOLD,
) -> ReadinessResult:
    """Localize target and gate on confidence threshold.

    Returns ReadinessResult with ready=True only when C ≥ threshold.
    """
    if not math.isfinite(float(threshold)) or not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be finite and between 0 and 1")
    result = localize(subgraph, runtime_graph, live_size)
    ready = result.confidence >= threshold
    return ReadinessResult(
        ready=ready,
        result=result,
        confidence=result.confidence,
        step_idx=-1,
    )


# ──────────────────────────────────────────────────────────────────────────── #
# Precheck pipeline (Appendix C)                                              #
# ──────────────────────────────────────────────────────────────────────────── #

class PrecheckPipeline:
    """Background thread that speculatively processes upcoming steps.

    After an action fires, the runner calls submit() with the current
    observation and dispatches the action. While the environment settles,
    the pipeline processes step N+1 (and optionally N+2). When the runner
    advances it calls try_get(step_idx) to retrieve the cached result.
    """

    def __init__(self, lookahead: int = PRECHECK_LOOKAHEAD):
        if not isinstance(lookahead, int) or isinstance(lookahead, bool) or lookahead < 0:
            raise ValueError("lookahead must be a non-negative integer")
        self._lookahead = lookahead
        self._cache: dict[int, ReadinessResult] = {}
        self._generation: dict[int, int] = {}
        self._lock = threading.Lock()
        self._executor = None
        self._queue: list[tuple[int, int, StepSubgraph, UIGraph, tuple[int, int]]] = []
        self._queue_lock = threading.Lock()
        self._stop_event = threading.Event()
        if self._lookahead > 0:
            self._executor = threading.Thread(target=self._worker, daemon=True)
            self._executor.start()

    def submit(
        self,
        step_idx: int,
        subgraphs: list[Optional[StepSubgraph]],
        current_graph: UIGraph,
        live_size: tuple[int, int],
    ) -> None:
        """Queue lookahead steps for background processing."""
        if self._lookahead <= 0 or self._stop_event.is_set():
            return
        for offset in range(1, self._lookahead + 1):
            nxt = step_idx + offset
            if nxt >= len(subgraphs) or subgraphs[nxt] is None:
                continue
            with self._lock:
                generation = self._generation.get(nxt, 0) + 1
                self._generation[nxt] = generation
                self._cache.pop(nxt, None)
            with self._queue_lock:
                self._queue = [item for item in self._queue if item[0] != nxt]
                self._queue.append((nxt, generation, subgraphs[nxt], current_graph, live_size))

    def try_get(self, step_idx: int) -> Optional[ReadinessResult]:
        """Consume a cached result if confidence is high enough, else return None."""
        with self._lock:
            result = self._cache.pop(step_idx, None)
        if result is not None and result.confidence >= PRECHECK_MIN_CONF:
            logger.debug(f"Precheck hit for step {step_idx}: conf={result.confidence:.3f}")
            return result
        return None

    def invalidate(self, step_idx: int) -> None:
        with self._lock:
            self._cache.pop(step_idx, None)
            self._generation[step_idx] = self._generation.get(step_idx, 0) + 1
        with self._queue_lock:
            self._queue = [item for item in self._queue if item[0] != step_idx]

    def _worker(self) -> None:
        while not self._stop_event.is_set():
            item = None
            with self._queue_lock:
                if self._queue:
                    item = self._queue.pop(0)
            if item is None:
                time.sleep(0.05)
                continue
            step_idx, generation, subgraph, graph, live_size = item
            try:
                res = check_readiness(subgraph, graph, live_size)
                res.step_idx = step_idx
                with self._lock:
                    if (
                        not self._stop_event.is_set()
                        and self._generation.get(step_idx) == generation
                    ):
                        self._cache[step_idx] = res
            except Exception as e:
                logger.debug(f"Precheck failed for step {step_idx}: {e}")

    def stop(self) -> None:
        self._stop_event.set()
        with self._queue_lock:
            self._queue.clear()
        if self._executor is not None:
            self._executor.join(timeout=1.0)
