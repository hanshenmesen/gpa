"""Isolated native-input recorder worker.

The Web service never imports or starts a native event listener in its own
process when this worker is enabled.  A crash in Quartz, PyObjC, or pynput then
terminates only this child process; the parent can report the failure and keep
the console available.
"""
from __future__ import annotations

import os
import pickle
from pathlib import Path
from typing import Any


def recorder_worker_main(connection, result_path: str, input_backend: str) -> None:
    from gpa.recording.recorder import Recorder

    recorder = Recorder(input_backend=input_backend)
    stopped = False
    try:
        recorder.start()
        connection.send({
            "ok": True,
            "event": "started",
            "pid": os.getpid(),
            "input_backend": recorder._input_capture_backend,
        })
        while True:
            try:
                message = connection.recv()
            except EOFError:
                break
            if not isinstance(message, dict):
                connection.send({"ok": False, "error": "Recorder command must be an object."})
                continue
            command = str(message.get("command") or "").strip().casefold()
            if command == "status":
                connection.send({
                    "ok": True,
                    "event": "status",
                    "event_count": len(recorder._recording.events),
                })
                continue
            if command == "append_external_event":
                payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
                try:
                    event = recorder.append_external_event(**payload)
                except (ValueError, TypeError, RuntimeError) as exc:
                    connection.send({
                        "ok": False,
                        "event": "command_error",
                        "error_type": "validation",
                        "error": str(exc),
                    })
                    continue
                connection.send({
                    "ok": True,
                    "event": "external_event",
                    "event_count": len(recorder._recording.events),
                    "recorded_event": {
                        "event_type": event.event_type,
                        "value": event.value,
                        "active_app": event.active_app,
                        "input_source": event.metadata.get("input_source"),
                    },
                })
                continue
            if command == "stop":
                recording = recorder.stop()
                stopped = True
                _write_recording_snapshot(Path(result_path), recording)
                connection.send({
                    "ok": True,
                    "event": "stopped",
                    "event_count": len(recording.events),
                })
                return
            if command == "abort":
                recorder.stop()
                stopped = True
                connection.send({"ok": True, "event": "aborted"})
                return
            connection.send({"ok": False, "error": f"Unknown recorder command: {command or '<empty>'}"})
    except BaseException as exc:
        try:
            connection.send({
                "ok": False,
                "event": "worker_error",
                "error": f"{type(exc).__name__}: {exc}",
            })
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        if not stopped:
            try:
                recorder.stop()
            except BaseException:
                pass
        try:
            connection.close()
        except OSError:
            pass


def _write_recording_snapshot(path: Path, recording: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        with temporary.open("xb") as stream:
            pickle.dump(recording, stream, protocol=pickle.HIGHEST_PROTOCOL)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
