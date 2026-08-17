"""Pure helpers for per-page console heartbeat leases."""
from __future__ import annotations

from typing import Any, MutableMapping


def active_clients(
    clients: MutableMapping[str, dict[str, Any]],
    *,
    now: float,
    timeout: float,
) -> list[dict[str, Any]]:
    return [
        item for item in clients.values()
        if float(item.get("last_seen_monotonic") or 0.0) > 0
        and now - float(item.get("last_seen_monotonic") or 0.0) <= timeout
    ]


def client_connected(
    clients: MutableMapping[str, dict[str, Any]],
    *,
    now: float,
    timeout: float,
    client_id: str = "",
) -> bool:
    if client_id:
        client = clients.get(client_id) or {}
        last_seen = float(client.get("last_seen_monotonic") or 0.0)
        return last_seen > 0 and now - last_seen <= timeout
    return bool(active_clients(clients, now=now, timeout=timeout))


def mark_client_seen(
    clients: MutableMapping[str, dict[str, Any]],
    *,
    client_id: str,
    environment: dict[str, Any] | None,
    now: float,
    seen_at: str,
    timeout: float,
) -> dict[str, Any]:
    stale_cutoff = now - timeout * 3
    for stale_id, stale in list(clients.items()):
        if float(stale.get("last_seen_monotonic") or 0.0) < stale_cutoff:
            clients.pop(stale_id, None)
    previous = dict(clients.get(client_id) or {})
    entry = {
        "id": client_id,
        "last_seen_monotonic": now,
        "last_seen_at": seen_at,
        "environment": dict(environment or previous.get("environment") or {}),
    }
    clients[client_id] = entry
    return entry


def disconnect_client(
    clients: MutableMapping[str, dict[str, Any]],
    client_id: str,
) -> None:
    if client_id:
        clients.pop(client_id, None)
    else:
        clients.clear()


def latest_active_client(
    clients: MutableMapping[str, dict[str, Any]],
    *,
    now: float,
    timeout: float,
) -> dict[str, Any]:
    active = active_clients(clients, now=now, timeout=timeout)
    return dict(
        max(active, key=lambda item: float(item.get("last_seen_monotonic") or 0.0))
        if active else {}
    )


def client_status(
    clients: MutableMapping[str, dict[str, Any]],
    *,
    fallback: dict[str, Any] | None,
    now: float,
    timeout: float,
) -> dict[str, Any]:
    active = active_clients(clients, now=now, timeout=timeout)
    client = dict(
        max(active, key=lambda item: float(item.get("last_seen_monotonic") or 0.0))
        if active else fallback or {}
    )
    last_seen = float(client.get("last_seen_monotonic") or 0.0)
    return {
        "id": client.get("id", ""),
        "connected": bool(active),
        "last_seen_at": client.get("last_seen_at", ""),
        "seconds_since_seen": round(now - last_seen, 2) if last_seen else None,
        "timeout_seconds": timeout,
        "active_client_count": len(active),
    }
