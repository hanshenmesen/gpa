"""Content-injection and deception defenses for GUI replay.

Real desktop replay reads on-screen text, browser page content, and clipboard
data — all of which are UNTRUSTED and can be crafted to mislead the agent
(environmental injection attacks, cf. arXiv:2509.11250 "Chameleon" and the
Trustworthy GUI Agents survey arXiv:2503.23434). Reported real-world misleading
rates reach ~42% (arXiv:2507.04227).

This module adds defense-in-depth on top of the executor's existing
self-interrupt safety (token / quarantine / watchdog in actions.py):

  1. A confirmation gate for irreversible actions (send / delete / pay …).
  2. A host-level allow-list for opened URLs.
  3. An allow-list for messaging recipients.

All gates are OFF by default (no configuration = no behavior change) so existing
deterministic replays are unaffected. Each gate activates only via its env var.
"""
from __future__ import annotations

import os
from typing import Optional
from urllib.parse import urlparse

REQUIRE_CONFIRM_IRREVERSIBLE_ENV = "GPA_REQUIRE_CONFIRM_IRREVERSIBLE"
ALLOWED_URL_HOSTS_ENV = "GPA_ALLOWED_URL_HOSTS"
ALLOWED_RECIPIENTS_ENV = "GPA_ALLOWED_RECIPIENTS"

# Truly destructive / irreversible intents. Kept focused to avoid false
# positives (e.g. plain "submit"/"confirm" are intentionally excluded).
IRREVERSIBLE_KEYWORDS: tuple[str, ...] = (
    "send",
    "delete",
    "remove",
    "discard",
    "pay",
    "payment",
    "purchase",
    "buy",
    "checkout",
    "transfer",
    "uninstall",
    "发送",
    "发出",
    "删除",
    "移除",
    "丢弃",
    "支付",
    "付款",
    "购买",
    "下单",
    "转账",
    "卸载",
)

# Send-style keys that finalize a message in a messaging/chat surface.
_SEND_HOTKEYS = {"cmd+enter", "command+enter", "ctrl+enter", "cmd+return"}


def _env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "") or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def _coerce_bool(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def require_irreversible_confirmation() -> bool:
    return _coerce_bool(os.environ.get(REQUIRE_CONFIRM_IRREVERSIBLE_ENV))


def allowed_url_hosts() -> list[str]:
    return [host.casefold().lstrip(".") for host in _env_list(ALLOWED_URL_HOSTS_ENV)]


def allowed_recipients() -> list[str]:
    return [name.casefold() for name in _env_list(ALLOWED_RECIPIENTS_ENV)]


def is_irreversible_action(action_type: str, action_text: str, value: str) -> bool:
    """Heuristically detect an irreversible/destructive step."""
    haystack = " ".join([str(action_text or ""), str(value or "")]).casefold()
    if any(keyword in haystack for keyword in IRREVERSIBLE_KEYWORDS):
        return True
    if action_type == "hotkey":
        combo = str(value or "").strip().casefold().replace("command", "cmd").replace("return", "enter")
        if combo in {c.replace("command", "cmd").replace("return", "enter") for c in _SEND_HOTKEYS}:
            return True
    return False


def _host_matches(host: str, patterns: list[str]) -> bool:
    host = str(host or "").casefold().lstrip(".")
    for pattern in patterns:
        if not pattern:
            continue
        if host == pattern or host.endswith("." + pattern):
            return True
    return False


def check_url_allowed(url: str) -> Optional[str]:
    """Return an error message if the URL host is not on the allow-list.

    No allow-list configured => all URLs allowed (returns None).
    """
    patterns = allowed_url_hosts()
    if not patterns:
        return None
    target = str(url or "").strip()
    if not target:
        return None
    if "://" not in target:
        target = "https://" + target
    host = (urlparse(target).hostname or "").casefold()
    if not host:
        return f"Blocked open_url: could not parse host from {url!r}."
    if not _host_matches(host, patterns):
        return (
            f"Blocked open_url: host {host!r} is not in the allow-list "
            f"({ALLOWED_URL_HOSTS_ENV}={','.join(patterns)})."
        )
    return None


def check_recipient_allowed(recipient: str) -> Optional[str]:
    """Return an error message if the recipient is not on the allow-list.

    No allow-list configured => all recipients allowed (returns None).
    """
    patterns = allowed_recipients()
    if not patterns:
        return None
    name = str(recipient or "").strip().casefold()
    if not name:
        return None
    if name not in patterns and not any(p in name or name in p for p in patterns):
        return (
            f"Blocked send: recipient {recipient!r} is not in the allow-list "
            f"({ALLOWED_RECIPIENTS_ENV})."
        )
    return None
