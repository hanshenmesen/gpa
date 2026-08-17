"""Strict parsing helpers for runtime environment configuration."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Mapping


class RuntimeConfigurationError(ValueError):
    """Raised when a runtime environment value is malformed or unsafe."""


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def env_bool(
    name: str,
    default: bool = False,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    values = os.environ if environ is None else environ
    raw = values.get(name)
    if raw is None or not str(raw).strip():
        return default
    normalized = str(raw).strip().casefold()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    accepted = "1/0, true/false, yes/no, or on/off"
    raise RuntimeConfigurationError(f"{name} must be {accepted}; got {raw!r}")


def env_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    environ: Mapping[str, str] | None = None,
) -> float:
    values = os.environ if environ is None else environ
    raw = values.get(name)
    if raw is None or not str(raw).strip():
        value = float(default)
    else:
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise RuntimeConfigurationError(f"{name} must be a number; got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise RuntimeConfigurationError(f"{name} must be at least {minimum}; got {value}")
    if maximum is not None and value > maximum:
        raise RuntimeConfigurationError(f"{name} must be at most {maximum}; got {value}")
    return value


def env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    values = os.environ if environ is None else environ
    raw = values.get(name)
    if raw is None or not str(raw).strip():
        value = int(default)
    else:
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise RuntimeConfigurationError(f"{name} must be an integer; got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise RuntimeConfigurationError(f"{name} must be at least {minimum}; got {value}")
    if maximum is not None and value > maximum:
        raise RuntimeConfigurationError(f"{name} must be at most {maximum}; got {value}")
    return value


def env_path(
    name: str,
    default: str | Path,
    *,
    base: str | Path,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve a configurable path without depending on the process cwd."""
    values = os.environ if environ is None else environ
    raw = str(values.get(name) or "").strip()
    path = Path(raw).expanduser() if raw else Path(default).expanduser()
    if not path.is_absolute():
        path = Path(base) / path
    return path


def user_data_path(
    app_name: str,
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: str | Path | None = None,
) -> Path:
    """Return a writable per-user data directory using OS conventions."""
    platform_name = sys.platform if platform_name is None else platform_name
    values = os.environ if environ is None else environ
    user_home = Path.home() if home is None else Path(home)

    if platform_name == "darwin":
        return user_home / "Library" / "Application Support" / app_name
    if platform_name.startswith("win"):
        local_app_data = str(values.get("LOCALAPPDATA") or "").strip()
        base = Path(local_app_data) if local_app_data else user_home / "AppData" / "Local"
        return base / app_name

    xdg_data_home = str(values.get("XDG_DATA_HOME") or "").strip()
    if xdg_data_home:
        base = Path(xdg_data_home).expanduser()
        if not base.is_absolute():
            raise RuntimeConfigurationError("XDG_DATA_HOME must be an absolute path")
    else:
        base = user_home / ".local" / "share"
    return base / app_name


def update_env_file(path: str | Path, updates: Mapping[str, str | None]) -> None:
    """Atomically update a small dotenv file without interpreting its contents."""
    target = Path(path)
    normalized: dict[str, str | None] = {}
    for raw_name, raw_value in updates.items():
        name = str(raw_name or "").strip()
        if not _ENV_NAME.fullmatch(name):
            raise RuntimeConfigurationError(f"Invalid environment variable name: {name!r}")
        if raw_value is None:
            normalized[name] = None
            continue
        value = str(raw_value)
        if "\n" in value or "\r" in value or "\x00" in value:
            raise RuntimeConfigurationError(f"{name} contains an unsupported control character")
        normalized[name] = value

    lines = target.read_text(encoding="utf-8").splitlines() if target.is_file() else []
    output: list[str] = []
    consumed: set[str] = set()
    for line in lines:
        candidate = line.lstrip()
        if candidate.startswith("export "):
            candidate = candidate[7:].lstrip()
        name, separator, _ = candidate.partition("=")
        name = name.strip()
        if separator and name in normalized:
            if name not in consumed and normalized[name] is not None:
                output.append(f"{name}={_dotenv_quote(normalized[name] or '')}")
            consumed.add(name)
            continue
        output.append(line)
    for name, value in normalized.items():
        if name not in consumed and value is not None:
            output.append(f"{name}={_dotenv_quote(value)}")
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_text(target, "\n".join(output).rstrip() + "\n")


def _dotenv_quote(value: str) -> str:
    if value and re.fullmatch(r"[A-Za-z0-9_./:@+,-]+", value):
        return value
    # The launcher sources this file in a shell. Single-quote the value and
    # split literal apostrophes so $, backticks, and command substitutions can
    # never execute when GPA is restarted.
    escaped = value.replace("'", "'\"'\"'")
    return f"'{escaped}'"


def _atomic_text(path: Path, content: str) -> None:
    import tempfile

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "RuntimeConfigurationError",
    "env_bool",
    "env_float",
    "env_int",
    "env_path",
    "update_env_file",
    "user_data_path",
]
