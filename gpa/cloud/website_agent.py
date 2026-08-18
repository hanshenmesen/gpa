"""Outbound-only connection from the local GPA Agent to the public website."""
from __future__ import annotations

import json
import os
import platform
import re
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from gpa import __version__
from gpa.cloud.agent_protocol import AgentProtocolError, parse_cloud_command
from gpa.cloud.service_config import CloudServiceConfig
from gpa.replay.environment import capture_environment, compare_environments
from gpa.runtime_config import user_data_path
from gpa.storage.workflow import Workflow, WorkflowStep, WorkflowStorage

MAX_RESPONSE_BYTES = 128 * 1024
Transport = Callable[[str, str, Mapping[str, Any] | None, str], dict[str, Any]]


class WebsiteAgentError(RuntimeError):
    """A safe, user-presentable cloud connection error."""


class AgentCredentialStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else user_data_path("GPA") / "cloud-device.json"

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return dict(value) if isinstance(value, Mapping) else {}

    def save(self, value: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=".cloud-device.", dir=self.path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(dict(value), stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class WebsiteAgentClient:
    def __init__(
        self,
        config: CloudServiceConfig | None = None,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.config = config or CloudServiceConfig.from_environment()
        self._transport = transport

    def start_pairing(self, *, label: str) -> dict[str, Any]:
        environment = capture_environment()
        return self.request("POST", "/api/agent/pair/start", {
            "label": str(label or "This device")[:80],
            "platform": _platform_id(),
            "architecture": platform.machine() or "unknown",
            "agent_version": __version__,
            "capabilities": _capabilities(environment),
        })

    def pairing_status(self, state: Mapping[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/api/agent/pair/status", {
            "pairing_id": state.get("pairing_id"),
            "device_secret": state.get("device_secret"),
        })

    def heartbeat(self, token: str) -> dict[str, Any]:
        environment = capture_environment()
        return self.request("POST", "/api/agent/heartbeat", {
            "agent_version": __version__,
            "capabilities": _capabilities(environment),
            "permissions": _permissions(environment),
            "environment": environment,
        }, token=token)

    def commands(self, token: str) -> list[dict[str, Any]]:
        result = self.request("GET", "/api/agent/commands", token=token)
        commands = result.get("commands")
        return [dict(item) for item in commands if isinstance(item, Mapping)] if isinstance(commands, list) else []

    def report(self, token: str, command_id: str, status: str, result: Mapping[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/api/agent/commands/{command_id}/result", {
            "status": status,
            "result": dict(result),
        }, token=token)

    def request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        token: str = "",
    ) -> dict[str, Any]:
        if self._transport:
            return self._transport(method, path, body, token)
        base_url = self.config.api_base_url or self.config.web_base_url
        url = f"{base_url}{'/' + str(path or '').lstrip('/')}"
        payload = None if body is None else json.dumps(dict(body), ensure_ascii=False).encode("utf-8")
        headers = {"accept": "application/json", "user-agent": f"GPA-Agent/{__version__}"}
        if payload is not None:
            headers["content-type"] = "application/json"
        if token:
            headers["authorization"] = f"Bearer {token}"
        request = Request(url, data=payload, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.config.connect_timeout_seconds) as response:
                final = urlsplit(response.geturl())
                if final.scheme != "https" and final.hostname not in {"127.0.0.1", "::1", "localhost"}:
                    raise WebsiteAgentError("Cloud service redirected to an insecure endpoint.")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise WebsiteAgentError("Cloud service response is too large.")
        except HTTPError as exc:
            raw = exc.read(MAX_RESPONSE_BYTES)
            try:
                detail = json.loads(raw.decode("utf-8")).get("error")
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                detail = None
            raise WebsiteAgentError(str(detail or f"Cloud service returned HTTP {exc.code}.")) from exc
        except (URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise WebsiteAgentError("Cannot reach the GPA website right now.") from exc
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebsiteAgentError("Cloud service returned invalid data.") from exc
        if not isinstance(result, Mapping):
            raise WebsiteAgentError("Cloud service returned invalid data.")
        return dict(result)


class CloudAgentService:
    """Maintains pairing, heartbeat, preflight inbox, and local-only approval."""

    def __init__(
        self,
        *,
        client: WebsiteAgentClient | None = None,
        credentials: AgentCredentialStore | None = None,
        inbox_path: str | Path | None = None,
        workflow_storage: WorkflowStorage | None = None,
    ) -> None:
        self.client = client or WebsiteAgentClient()
        self.credentials = credentials or AgentCredentialStore()
        self.inbox_path = Path(inbox_path) if inbox_path else user_data_path("GPA") / "cloud-inbox.json"
        self.workflow_storage = workflow_storage or WorkflowStorage()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error = ""
        self._last_sync = 0.0

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="gpa-cloud-agent", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def begin_pairing(self, label: str = "") -> dict[str, Any]:
        pairing = self.client.start_pairing(label=label or platform.node() or "This device")
        state = {
            "status": "pending",
            "pairing_id": pairing["pairing_id"],
            "device_secret": pairing["device_secret"],
            "device_token": pairing["device_token"],
            "claim_url": pairing["claim_url"],
            "expires_at": pairing["expires_at"],
            "label": label or platform.node() or "This device",
        }
        self.credentials.save(state)
        return self.status()

    def poll_pairing(self) -> dict[str, Any]:
        state = self.credentials.load()
        if state.get("status") != "pending":
            return self.status()
        result = self.client.pairing_status(state)
        if result.get("status") == "paired":
            state = {
                "status": "active",
                "device_id": result.get("device_id"),
                "device_token": state.get("device_token"),
                "label": result.get("label") or state.get("label"),
                "paired_at": int(time.time() * 1000),
            }
            self.credentials.save(state)
            self.sync_once()
        return self.status()

    def disconnect(self) -> None:
        self.credentials.clear()

    def status(self) -> dict[str, Any]:
        state = self.credentials.load()
        inbox = self._load_inbox()
        return {
            "status": state.get("status") or "disconnected",
            "web_base_url": self.client.config.web_base_url,
            "device_id": state.get("device_id") or "",
            "label": state.get("label") or "",
            "claim_url": state.get("claim_url") or "",
            "expires_at": state.get("expires_at") or 0,
            "last_sync_at": int(self._last_sync * 1000) if self._last_sync else 0,
            "last_error": self._last_error,
            "inbox": inbox,
        }

    def sync_once(self) -> dict[str, Any]:
        state = self.credentials.load()
        if state.get("status") == "pending":
            return self.poll_pairing()
        if state.get("status") != "active" or not state.get("device_token") or not state.get("device_id"):
            return self.status()
        token = str(state["device_token"])
        self.client.heartbeat(token)
        known = {str(item.get("command_id")) for item in self._load_inbox()}
        for raw in self.client.commands(token):
            if str(raw.get("command_id")) in known:
                continue
            self._prepare_command(raw, state)
        self._last_error = ""
        self._last_sync = time.time()
        return self.status()

    def accept(self, command_id: str) -> dict[str, Any]:
        return self._decide(command_id, accepted=True)

    def decline(self, command_id: str) -> dict[str, Any]:
        return self._decide(command_id, accepted=False)

    def _prepare_command(self, raw: Mapping[str, Any], state: Mapping[str, Any]) -> None:
        token = str(state.get("device_token") or "")
        try:
            command = parse_cloud_command(raw, expected_device_id=str(state["device_id"]))
            if command.command_type != "replay.prepare":
                raise AgentProtocolError("Only Replay preflight is enabled in this preview.")
            metadata = dict(command.metadata or {})
            compatibility = _compatibility(metadata)
            entry = {
                "command_id": command.command_id,
                "replay_id": command.replay_id,
                "title": str(metadata.get("title") or command.replay_id),
                "description": str(metadata.get("description") or ""),
                "status": compatibility["status"],
                "compatibility": compatibility,
                "payload": metadata,
                "received_at": int(time.time() * 1000),
            }
            self._upsert_inbox(entry)
            outcome = "blocked" if compatibility["status"] == "blocked" else "compatible"
            self.client.report(token, command.command_id, outcome, compatibility)
        except (AgentProtocolError, ValueError, TypeError) as exc:
            command_id = str(raw.get("command_id") or "")
            if re.fullmatch(r"[0-9a-fA-F-]{36}", command_id):
                self.client.report(token, command_id, "failed", {"reason": str(exc)[:500]})

    def _decide(self, command_id: str, *, accepted: bool) -> dict[str, Any]:
        state = self.credentials.load()
        if state.get("status") != "active":
            raise WebsiteAgentError("This device is not connected.")
        inbox = self._load_inbox()
        entry = next((item for item in inbox if item.get("command_id") == command_id), None)
        if not entry:
            raise WebsiteAgentError("Cloud Replay is not in the local inbox.")
        if entry.get("status") not in {"compatible", "degraded", "blocked"}:
            raise WebsiteAgentError("Cloud Replay has already been reviewed.")
        if accepted and entry.get("status") == "blocked":
            raise WebsiteAgentError("Blocked Replay cannot be imported until compatibility issues are resolved.")
        result: dict[str, Any] = {"local_confirmation": accepted}
        if accepted:
            workflow_id = self._import_draft(entry)
            entry["status"] = "accepted"
            entry["workflow_id"] = workflow_id
            result["workflow_id"] = workflow_id
        else:
            entry["status"] = "declined"
        self._write_inbox(inbox)
        self.client.report(str(state["device_token"]), command_id, "accepted" if accepted else "declined", result)
        return self.status()

    def _import_draft(self, entry: Mapping[str, Any]) -> str:
        payload = entry.get("payload") if isinstance(entry.get("payload"), Mapping) else {}
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(entry.get("replay_id") or "cloud-replay")).strip("-")[:70]
        workflow_id = f"cloud-{slug}-{str(entry['command_id'])[:8]}"
        raw_steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
        steps = [
            WorkflowStep(
                step_number=index,
                action=str(item.get("title") or f"Review step {index}"),
                action_type="manual_review",
                metadata={"success": str(item.get("success") or ""), "cloud_draft": True},
            )
            for index, item in enumerate(raw_steps, 1)
            if isinstance(item, Mapping)
        ]
        workflow = Workflow(
            workflow_id=workflow_id,
            workflow_name=str(payload.get("title") or entry.get("title") or workflow_id),
            workflow_title=str(payload.get("title") or entry.get("title") or workflow_id),
            description=str(payload.get("description") or entry.get("description") or ""),
            task_description=str(payload.get("description") or ""),
            category=str(payload.get("collection") or "Cloud draft"),
            steps=steps,
            provenance={"source": "gpa-online", "command_id": entry["command_id"], "author": payload.get("author")},
            environment=dict(payload.get("recorded_environment") or {}) if isinstance(payload.get("recorded_environment"), Mapping) else {},
            understanding={"execution_ready": False, "review_required": True, "imported_as": "draft"},
        )
        self.workflow_storage.save(workflow)
        return workflow_id

    def _load_inbox(self) -> list[dict[str, Any]]:
        if not self.inbox_path.is_file():
            return []
        try:
            value = json.loads(self.inbox_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [dict(item) for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []

    def _upsert_inbox(self, entry: Mapping[str, Any]) -> None:
        inbox = [item for item in self._load_inbox() if item.get("command_id") != entry.get("command_id")]
        inbox.insert(0, dict(entry))
        self._write_inbox(inbox[:100])

    def _write_inbox(self, inbox: list[dict[str, Any]]) -> None:
        self.inbox_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=".cloud-inbox.", dir=self.inbox_path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(inbox, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.inbox_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _run(self) -> None:
        while not self._stop.wait(5.0):
            if not self.credentials.load():
                continue
            try:
                self.sync_once()
            except WebsiteAgentError as exc:
                self._last_error = str(exc)
            except Exception:
                self._last_error = "Cloud sync failed safely; local automation remains unchanged."


def _platform_id() -> str:
    return "darwin" if platform.system().casefold() == "darwin" else platform.system().casefold() or "unknown"


def _capabilities(environment: Mapping[str, Any]) -> dict[str, bool]:
    safety = environment.get("input_safety") if isinstance(environment.get("input_safety"), Mapping) else {}
    return {"browser": True, "desktop_automation": safety.get("desktop_automation_enabled") is True, "local_confirmation": True}


def _permissions(environment: Mapping[str, Any]) -> dict[str, str]:
    enabled = _capabilities(environment)["desktop_automation"]
    return {"desktop_automation": "granted" if enabled else "denied", "local_confirmation": "granted"}


def _compatibility(payload: Mapping[str, Any]) -> dict[str, Any]:
    environment = capture_environment()
    capabilities = _capabilities(environment)
    reasons: list[str] = []
    supported = payload.get("supported_platforms")
    if isinstance(supported, list) and _platform_id() not in {str(item) for item in supported}:
        reasons.append("当前操作系统不在该 Replay 的支持范围内。")
    required = payload.get("required_capabilities")
    if isinstance(required, list):
        missing = [str(item) for item in required if not capabilities.get(str(item), False)]
        if missing:
            reasons.append("缺少本机能力：" + "、".join(missing))
    recorded = payload.get("recorded_environment") if isinstance(payload.get("recorded_environment"), Mapping) else {}
    difference = compare_environments(recorded, environment)
    if difference.get("status") == "blocked":
        reasons.append("录制环境与当前主机存在阻断差异。")
    return {
        "status": "blocked" if reasons else "degraded" if difference.get("status") in {"degraded", "unknown"} else "compatible",
        "reasons": reasons,
        "environment_diff": difference,
        "current_environment": environment,
        "capabilities": capabilities,
        "requires_local_confirmation": True,
    }


__all__ = ["AgentCredentialStore", "CloudAgentService", "WebsiteAgentClient", "WebsiteAgentError"]
