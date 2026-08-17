"""Parent-side client for the isolated recorder worker."""
from __future__ import annotations

import math
import multiprocessing
import os
import pickle
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from gpa.recording.worker import recorder_worker_main


class RecorderWorkerError(RuntimeError):
    pass


def _validated_timeout(value: float, field: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{field} must be a finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{field} must be a finite number") from exc
    if not math.isfinite(parsed) or parsed <= 0 or parsed > 300:
        raise ValueError(f"{field} must be greater than 0 and at most 300 seconds")
    return parsed


def _event_count(response: dict, *, fallback: int = 0) -> int:
    raw = response.get("event_count", fallback)
    if isinstance(raw, bool):
        raise RecorderWorkerError("Recorder worker returned an invalid event count.")
    try:
        count = int(raw)
    except (TypeError, ValueError) as exc:
        raise RecorderWorkerError("Recorder worker returned an invalid event count.") from exc
    if count < 0:
        raise RecorderWorkerError("Recorder worker returned an invalid event count.")
    return count


@dataclass
class WorkerRecordedEvent:
    event_type: str = ""
    value: str = ""
    active_app: str = ""
    metadata: dict[str, Any] = None

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {}


class RecorderWorkerClient:
    def __init__(
        self,
        *,
        input_backend: str,
        context_name: str = "spawn",
        worker_target: Optional[Callable] = None,
        start_timeout: float = 6.0,
        command_timeout: float = 12.0,
    ) -> None:
        self._input_capture_backend = str(input_backend or "auto")
        self._context = multiprocessing.get_context(context_name)
        self._worker_target = worker_target or recorder_worker_main
        self._start_timeout = _validated_timeout(start_timeout, "start_timeout")
        self._command_timeout = _validated_timeout(command_timeout, "command_timeout")
        self._command_lock = threading.Lock()
        self._parent_connection = None
        self._process = None
        self._temporary_root: Optional[Path] = None
        self._result_path: Optional[Path] = None
        self._event_count = 0
        self._closed = False

    @property
    def event_count(self) -> int:
        return self._event_count

    @property
    def worker_pid(self) -> int:
        return int(getattr(self._process, "pid", 0) or 0)

    @property
    def worker_exit_code(self) -> Optional[int]:
        return getattr(self._process, "exitcode", None)

    def start(self) -> None:
        if self._process is not None:
            raise RuntimeError("Recorder worker is already started.")
        self._temporary_root = Path(tempfile.mkdtemp(prefix="gpa-recorder-worker-"))
        os.chmod(self._temporary_root, 0o700)
        self._result_path = self._temporary_root / "recording.snapshot"
        parent, child = self._context.Pipe(duplex=True)
        self._parent_connection = parent
        self._process = self._context.Process(
            target=self._worker_target,
            args=(child, str(self._result_path), self._input_capture_backend),
            name="gpa-native-recorder",
            daemon=True,
        )
        try:
            self._process.start()
            child.close()
            response = self._receive(self._start_timeout)
            if not response.get("ok") or response.get("event") != "started":
                raise RecorderWorkerError(response.get("error") or "Recorder worker failed to start.")
            self._input_capture_backend = str(
                response.get("input_backend") or self._input_capture_backend
            )
        except BaseException:
            self.close()
            raise

    def is_alive(self) -> bool:
        return bool(self._process is not None and self._process.is_alive())

    def failure_reason(self) -> str:
        code = self.worker_exit_code
        return (
            "Recorder worker exited unexpectedly"
            + (f" with code {code}." if code is not None else ".")
            + " The GPA console stayed online and native input hooks were released with the worker."
        )

    def refresh_event_count(self, *, timeout: float = 0.5) -> int:
        if not self.is_alive():
            raise RecorderWorkerError(self.failure_reason())
        response = self._request(
            {"command": "status"}, timeout=timeout, expected_event="status"
        )
        self._event_count = _event_count(response, fallback=self._event_count)
        return self._event_count

    def append_external_event(self, event_type: str, **payload) -> WorkerRecordedEvent:
        response = self._request(
            {
                "command": "append_external_event",
                "payload": {"event_type": event_type, **payload},
            },
            expected_event="external_event",
        )
        self._event_count = _event_count(response)
        event = dict(response.get("recorded_event") or {})
        return WorkerRecordedEvent(
            event_type=str(event.get("event_type") or ""),
            value=str(event.get("value") or ""),
            active_app=str(event.get("active_app") or ""),
            metadata={"input_source": str(event.get("input_source") or "")},
        )

    def stop(self):
        response = self._request(
            {"command": "stop"},
            timeout=max(self._command_timeout, 20.0),
            expected_event="stopped",
        )
        self._event_count = _event_count(response)
        process = self._process
        if process is not None:
            process.join(timeout=3.0)
            if process.is_alive():
                self.close()
                raise RecorderWorkerError("Recorder worker did not exit after stopping.")
        if self._result_path is None or not self._result_path.is_file():
            self.close()
            raise RecorderWorkerError("Recorder worker stopped without a recording snapshot.")
        try:
            with self._result_path.open("rb") as stream:
                recording = pickle.load(stream)
            from gpa.recording.recorder import Recording

            if not isinstance(recording, Recording):
                raise TypeError(f"expected Recording, got {type(recording).__name__}")
        except Exception as exc:
            raise RecorderWorkerError(f"Could not read recorder worker snapshot: {exc}") from exc
        finally:
            self._cleanup_resources(terminate=False)
        return recording

    def close(self) -> None:
        if self._closed and self._process is None:
            return
        process = self._process
        if process is not None and process.is_alive():
            try:
                self._request({"command": "abort"}, timeout=1.0, expected_event="aborted")
            except Exception:
                pass
            process.join(timeout=1.0)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1.0)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(timeout=1.0)
        self._cleanup_resources(terminate=False)

    def _request(
        self,
        message: dict,
        *,
        timeout: Optional[float] = None,
        expected_event: str = "",
    ) -> dict:
        with self._command_lock:
            if not self.is_alive() or self._parent_connection is None:
                raise RecorderWorkerError(self.failure_reason())
            response_timeout = (
                self._command_timeout
                if timeout is None
                else _validated_timeout(timeout, "timeout")
            )
            try:
                self._parent_connection.send(message)
            except (BrokenPipeError, EOFError, OSError) as exc:
                raise RecorderWorkerError(self.failure_reason()) from exc
            try:
                response = self._receive(response_timeout)
            except TimeoutError as exc:
                self._terminate_unresponsive_worker()
                raise RecorderWorkerError(
                    "Recorder worker became unresponsive and was terminated; the GPA console stayed online."
                ) from exc
            if not response.get("ok"):
                if response.get("error_type") == "validation":
                    raise ValueError(str(response.get("error") or "Recorder event was rejected."))
                raise RecorderWorkerError(str(response.get("error") or "Recorder worker command failed."))
            if expected_event and response.get("event") != expected_event:
                self._terminate_unresponsive_worker()
                raise RecorderWorkerError(
                    "Recorder worker protocol mismatch: "
                    f"expected {expected_event!r}, got {response.get('event')!r}."
                )
            return response

    def _receive(self, timeout: float) -> dict:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            connection = self._parent_connection
            if connection is None:
                raise RecorderWorkerError(self.failure_reason())
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if not self.is_alive():
                    raise RecorderWorkerError(self.failure_reason())
                raise TimeoutError("Timed out waiting for recorder worker.")
            try:
                if connection.poll(min(0.05, remaining)):
                    response = connection.recv()
                    return dict(response) if isinstance(response, dict) else {
                        "ok": False,
                        "error": "Recorder worker returned an invalid response.",
                    }
            except (BrokenPipeError, EOFError, OSError) as exc:
                raise RecorderWorkerError(self.failure_reason()) from exc
            if self._process is not None and not self._process.is_alive():
                raise RecorderWorkerError(self.failure_reason())

    def _terminate_unresponsive_worker(self) -> None:
        process = self._process
        if process is not None and process.is_alive():
            process.terminate()
            process.join(timeout=1.0)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(timeout=1.0)

    def _cleanup_resources(self, *, terminate: bool) -> None:
        connection = self._parent_connection
        self._parent_connection = None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        self._process = None
        root = self._temporary_root
        self._temporary_root = None
        self._result_path = None
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)
        self._closed = True
