"""Small, dependency-free operational controls for GPA Cloud."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    retry_after_seconds: int = 0


class SlidingWindowLimiter:
    """Bounded per-client sliding-window limiter for one service process."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._calls = 0

    def check(self, client: str, bucket: str, *, limit: int, window_seconds: int = 60) -> LimitDecision:
        now = self._clock()
        key = (str(client)[:128], str(bucket)[:80])
        with self._lock:
            self._calls += 1
            events = self._events[key]
            cutoff = now - window_seconds
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                retry = max(1, int(window_seconds - (now - events[0]) + 0.999))
                return LimitDecision(False, retry)
            events.append(now)
            if self._calls % 1000 == 0:
                self._events = defaultdict(
                    deque,
                    {stored_key: stamps for stored_key, stamps in self._events.items() if stamps and stamps[-1] > cutoff},
                )
        return LimitDecision(True, 0)


class OperationalTelemetry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = time.time()
        self._requests = 0
        self._rate_limited = 0
        self._payload_rejected = 0
        self._errors = 0
        self._latency_ms_total = 0.0
        self._status_families: dict[str, int] = defaultdict(int)

    def observe(
        self,
        *,
        status_code: int,
        latency_ms: float,
        rate_limited: bool = False,
        payload_rejected: bool = False,
    ) -> None:
        with self._lock:
            self._requests += 1
            self._latency_ms_total += max(0.0, float(latency_ms))
            self._status_families[f"{max(0, int(status_code)) // 100}xx"] += 1
            self._rate_limited += int(rate_limited)
            self._payload_rejected += int(payload_rejected)
            self._errors += int(status_code >= 500)

    def prometheus(self) -> str:
        with self._lock:
            lines = [
                "# HELP gpa_cloud_uptime_seconds Process uptime.",
                "# TYPE gpa_cloud_uptime_seconds gauge",
                f"gpa_cloud_uptime_seconds {max(0, int(time.time() - self._started))}",
                "# HELP gpa_cloud_requests_total HTTP requests handled.",
                "# TYPE gpa_cloud_requests_total counter",
                f"gpa_cloud_requests_total {self._requests}",
                f"gpa_cloud_rate_limited_total {self._rate_limited}",
                f"gpa_cloud_payload_rejected_total {self._payload_rejected}",
                f"gpa_cloud_server_errors_total {self._errors}",
                f"gpa_cloud_request_latency_ms_total {self._latency_ms_total:.3f}",
            ]
            for family, count in sorted(self._status_families.items()):
                lines.append(f'gpa_cloud_responses_total{{family="{family}"}} {count}')
        return "\n".join(lines) + "\n"


def client_fingerprint(address: str, *, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{address}".encode("utf-8")).hexdigest()[:16]


def structured_access_log(**fields: object) -> None:
    """Emit one bounded JSON event without headers, query strings, or bodies."""
    safe = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": "http_request",
        **{str(key)[:64]: value for key, value in fields.items()},
    }
    print(json.dumps(safe, ensure_ascii=True, separators=(",", ":")), flush=True)


__all__ = [
    "LimitDecision",
    "OperationalTelemetry",
    "SlidingWindowLimiter",
    "client_fingerprint",
    "structured_access_log",
]
