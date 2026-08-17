"""Fail-closed protocol boundary between GPA Cloud and a local host agent.

The hosted service may coordinate work, but it must never manufacture local
desktop authority. Commands that can observe or mutate the desktop require a
fresh approval identifier minted by the local agent after user review.
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from gpa import __version__

AGENT_PROTOCOL_VERSION = "1.0"
MAX_COMMAND_TTL_SECONDS = 5 * 60
MAX_METADATA_ITEMS = 32
MAX_TEXT_LENGTH = 2048

READ_ONLY_COMMANDS = frozenset(
    {
        "agent.status",
        "replay.inspect",
        "replay.prepare",
        "diagnostics.collect",
    }
)
LOCAL_APPROVAL_COMMANDS = frozenset(
    {
        "recording.start",
        "recording.stop",
        "replay.run",
        "replay.stop",
    }
)
ALLOWED_COMMANDS = READ_ONLY_COMMANDS | LOCAL_APPROVAL_COMMANDS
_APPROVAL_ID = re.compile(r"^approval_[A-Za-z0-9_-]{16,160}$")


class AgentProtocolError(ValueError):
    """Raised when a cloud message is malformed, stale, or over-authorized."""


@dataclass(frozen=True)
class AgentCommand:
    command_id: str
    command_type: str
    device_id: str
    issued_at: float
    expires_at: float
    replay_id: str = ""
    local_approval_id: str = ""
    metadata: dict[str, str] | None = None


def command_requires_local_approval(command_type: str) -> bool:
    return str(command_type or "").strip() in LOCAL_APPROVAL_COMMANDS


def build_agent_hello(
    *,
    device_id: str,
    platform: str,
    platform_release: str,
    architecture: str,
    capabilities: Mapping[str, bool],
    permissions: Mapping[str, str],
) -> dict[str, Any]:
    """Build the non-secret capability advertisement sent after authentication."""
    return {
        "schema": "gpa.host-agent-hello/v1",
        "protocol_version": AGENT_PROTOCOL_VERSION,
        "agent_version": __version__,
        "device_id": _bounded_identifier(device_id, "device_id"),
        "platform": _bounded_text(platform, "platform", limit=80),
        "platform_release": _bounded_text(
            platform_release, "platform_release", limit=120
        ),
        "architecture": _bounded_text(architecture, "architecture", limit=80),
        "capabilities": {
            _bounded_identifier(name, "capability"): bool(enabled)
            for name, enabled in sorted(capabilities.items())
        },
        "permissions": {
            _bounded_identifier(name, "permission"): _permission_state(state)
            for name, state in sorted(permissions.items())
        },
    }


def parse_cloud_command(
    payload: Mapping[str, Any],
    *,
    expected_device_id: str,
    now: float | None = None,
) -> AgentCommand:
    """Validate a signed/authenticated cloud command before local dispatch.

    Signature and transport authentication belong to the connection layer.
    This parser enforces semantic authorization and freshness afterwards.
    """
    if not isinstance(payload, Mapping):
        raise AgentProtocolError("Cloud command must be an object.")
    if payload.get("schema") != "gpa.host-agent-command/v1":
        raise AgentProtocolError("Unsupported host-agent command schema.")
    if payload.get("protocol_version") != AGENT_PROTOCOL_VERSION:
        raise AgentProtocolError("Unsupported host-agent protocol version.")

    command_id = _uuid(payload.get("command_id"), "command_id")
    command_type = str(payload.get("command_type") or "").strip()
    if command_type not in ALLOWED_COMMANDS:
        raise AgentProtocolError(f"Unsupported cloud command: {command_type!r}")

    device_id = _bounded_identifier(payload.get("device_id"), "device_id")
    if device_id != _bounded_identifier(expected_device_id, "expected_device_id"):
        raise AgentProtocolError("Cloud command targets another device.")

    issued_at = _timestamp(payload.get("issued_at"), "issued_at")
    expires_at = _timestamp(payload.get("expires_at"), "expires_at")
    current = time.time() if now is None else float(now)
    if issued_at > current + 30:
        raise AgentProtocolError("Cloud command was issued in the future.")
    if expires_at <= current:
        raise AgentProtocolError("Cloud command has expired.")
    if expires_at <= issued_at or expires_at - issued_at > MAX_COMMAND_TTL_SECONDS:
        raise AgentProtocolError("Cloud command expiry exceeds the allowed window.")

    replay_id = str(payload.get("replay_id") or "").strip()
    if command_type.startswith("replay.") and not replay_id:
        raise AgentProtocolError("Replay commands require replay_id.")
    if replay_id:
        replay_id = _bounded_identifier(replay_id, "replay_id")

    local_approval_id = str(payload.get("local_approval_id") or "").strip()
    if command_requires_local_approval(command_type):
        if not _APPROVAL_ID.fullmatch(local_approval_id):
            raise AgentProtocolError(
                "Desktop-observing or mutating commands require fresh local approval."
            )
    elif local_approval_id:
        raise AgentProtocolError("Read-only commands must not carry desktop authority.")

    metadata = _metadata(payload.get("metadata"))
    return AgentCommand(
        command_id=command_id,
        command_type=command_type,
        device_id=device_id,
        issued_at=issued_at,
        expires_at=expires_at,
        replay_id=replay_id,
        local_approval_id=local_approval_id,
        metadata=metadata,
    )


def _uuid(value: Any, field: str) -> str:
    try:
        return str(uuid.UUID(str(value or "")))
    except (ValueError, TypeError, AttributeError) as exc:
        raise AgentProtocolError(f"{field} must be a UUID.") from exc


def _timestamp(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise AgentProtocolError(f"{field} must be a Unix timestamp.")
    try:
        timestamp = float(value)
    except (TypeError, ValueError) as exc:
        raise AgentProtocolError(f"{field} must be a Unix timestamp.") from exc
    if not 0 < timestamp < 10_000_000_000:
        raise AgentProtocolError(f"{field} is outside the supported range.")
    return timestamp


def _bounded_identifier(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", text):
        raise AgentProtocolError(f"{field} is invalid.")
    return text


def _bounded_text(value: Any, field: str, *, limit: int = MAX_TEXT_LENGTH) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit or any(char in text for char in "\r\n\x00"):
        raise AgentProtocolError(f"{field} is invalid.")
    return text


def _permission_state(value: Any) -> str:
    state = str(value or "").strip().casefold()
    if state not in {"granted", "denied", "unknown", "not_applicable"}:
        raise AgentProtocolError(f"Unsupported permission state: {state!r}")
    return state


def _metadata(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or len(value) > MAX_METADATA_ITEMS:
        raise AgentProtocolError("metadata must be a small object.")
    return {
        _bounded_identifier(key, "metadata key"): _bounded_text(item, "metadata value")
        for key, item in value.items()
    }


__all__ = [
    "AGENT_PROTOCOL_VERSION",
    "ALLOWED_COMMANDS",
    "LOCAL_APPROVAL_COMMANDS",
    "AgentCommand",
    "AgentProtocolError",
    "build_agent_hello",
    "command_requires_local_approval",
    "parse_cloud_command",
]
