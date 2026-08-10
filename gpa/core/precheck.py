"""Readiness checker and precheck pipeline.

Combines SMC localization confidence into a readiness gate before
each action. Wraps the precheck background-thread pipeline from
Appendix C of the paper.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from gpa.config import (
    READINESS_THRESHOLD, MAX_RETRIES, RETRY_SLEEP,
    PRECHECK_LOOKAHEAD, PRECHECK_MIN_CONF,
)
from gpa.core.smc import LocalizationResult, localize
from gpa.core.ui_graph import UIGraph, StepSubgraph

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
        self._lookahead = lookahead
        self._cache: dict[int, ReadinessResult] = {}
        self._lock = threading.Lock()
        self._executor = None
        self._queue: list[tuple[int, StepSubgraph, UIGraph, tuple[int, int]]] = []
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
        if self._lookahead <= 0:
            return
        with self._queue_lock:
            for offset in range(1, self._lookahead + 1):
                nxt = step_idx + offset
                if nxt < len(subgraphs) and subgraphs[nxt] is not None:
                    self._queue.append((nxt, subgraphs[nxt], current_graph, live_size))

    def try_get(self, step_idx: int) -> Optional[ReadinessResult]:
        """Return cached result if confidence is high enough, else None."""
        with self._lock:
            result = self._cache.get(step_idx)
        if result is not None and result.confidence >= PRECHECK_MIN_CONF:
            logger.debug(f"Precheck hit for step {step_idx}: conf={result.confidence:.3f}")
            return result
        return None

    def invalidate(self, step_idx: int) -> None:
        with self._lock:
            self._cache.pop(step_idx, None)

    def _worker(self) -> None:
        while not self._stop_event.is_set():
            item = None
            with self._queue_lock:
                if self._queue:
                    item = self._queue.pop(0)
            if item is None:
                time.sleep(0.05)
                continue
            step_idx, subgraph, graph, live_size = item
            try:
                res = check_readiness(subgraph, graph, live_size)
                res.step_idx = step_idx
                with self._lock:
                    self._cache[step_idx] = res
            except Exception as e:
                logger.debug(f"Precheck failed for step {step_idx}: {e}")

    def stop(self) -> None:
        self._stop_event.set()
        with self._queue_lock:
            self._queue.clear()
        if self._executor is not None:
            self._executor.join(timeout=1.0)
