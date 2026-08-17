"""Fail-closed public release checks for GPA desktop and cloud artifacts."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
from dataclasses import asdict, dataclass
from typing import Callable, Mapping


@dataclass(frozen=True)
class Finding:
    component: str
    name: str
    status: str
    detail: str

    @property
    def blocking(self) -> bool:
        return self.status == "blocked"


def _present(value: str | None) -> bool:
    return bool(str(value or "").strip())


def desktop_findings(
    *,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    module_available: Callable[[str], bool] | None = None,
    system: str | None = None,
    allow_unsigned_preview: bool = False,
) -> list[Finding]:
    env = environ or os.environ
    has_module = module_available or (lambda name: importlib.util.find_spec(name) is not None)
    findings: list[Finding] = []
    resolved_system = system or platform.system()
    findings.append(
        Finding(
            "desktop",
            "build_host",
            "ready" if resolved_system == "Darwin" else "blocked",
            "macOS build host" if resolved_system == "Darwin" else "macOS builds require macOS",
        )
    )
    for command in ("sips", "iconutil", "codesign", "hdiutil", "xcrun"):
        findings.append(
            Finding(
                "desktop",
                command,
                "ready" if which(command) else "blocked",
                "available" if which(command) else "command not found",
            )
        )
    for module in ("webview", "PyInstaller"):
        findings.append(
            Finding(
                "desktop",
                module,
                "ready" if has_module(module) else "blocked",
                "installed" if has_module(module) else "Python package not installed",
            )
        )
    for variable, label in (
        ("GPA_MACOS_SIGNING_IDENTITY", "Developer ID Application certificate"),
        ("GPA_MACOS_NOTARY_PROFILE", "notarytool keychain profile"),
    ):
        findings.append(
            Finding(
                "desktop",
                variable,
                (
                    "ready"
                    if _present(env.get(variable))
                    else "warning"
                    if allow_unsigned_preview
                    else "blocked"
                ),
                (
                    label
                    if _present(env.get(variable))
                    else f"missing {label}; unsigned technical preview only"
                    if allow_unsigned_preview
                    else f"missing {label}"
                ),
            )
        )
    return findings


def cloud_findings(
    *,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> list[Finding]:
    env = environ or os.environ
    required = (
        ("GPA_CLOUD_SERVER_PUBLIC_ORIGIN", "public HTTPS API origin"),
        ("GPA_CLOUD_SERVER_DATABASE_URL", "PostgreSQL connection"),
        ("GPA_CLOUD_SERVER_SESSION_SIGNING_KEY", "session signing secret"),
        ("GPA_CLOUD_SERVER_IDENTITY_ISSUER", "OIDC identity issuer"),
        ("GPA_CLOUD_SERVER_IDENTITY_AUDIENCE", "OIDC audience"),
        ("GPA_CLOUD_SERVER_IDENTITY_JWKS_URL", "OIDC JWKS endpoint"),
        ("GPA_CLOUD_SERVER_OBJECT_STORAGE_ENDPOINT", "object storage endpoint"),
        ("GPA_CLOUD_SERVER_OBJECT_STORAGE_BUCKET", "object storage bucket"),
    )
    findings = [
        Finding(
            "cloud",
            variable,
            "ready" if _present(env.get(variable)) else "blocked",
            label if _present(env.get(variable)) else f"missing {label}",
        )
        for variable, label in required
    ]
    origin = str(env.get("GPA_CLOUD_SERVER_PUBLIC_ORIGIN") or "")
    if origin and not origin.startswith("https://"):
        findings.append(Finding("cloud", "https", "blocked", "public origin must use HTTPS"))
    signing_key = str(env.get("GPA_CLOUD_SERVER_SESSION_SIGNING_KEY") or "")
    if signing_key and len(signing_key) < 32:
        findings.append(Finding("cloud", "signing_key_length", "blocked", "must be 32+ chars"))
    for variable in (
        "GPA_CLOUD_SERVER_IDENTITY_ISSUER",
        "GPA_CLOUD_SERVER_IDENTITY_JWKS_URL",
        "GPA_CLOUD_SERVER_OBJECT_STORAGE_ENDPOINT",
    ):
        value = str(env.get(variable) or "")
        if value and not value.startswith("https://"):
            findings.append(Finding("cloud", variable + "_https", "blocked", "must use HTTPS"))
    for command in ("docker", "pg_dump", "pg_restore"):
        findings.append(
            Finding(
                "cloud",
                command,
                "ready" if which(command) else "blocked",
                "available" if which(command) else "command not found",
            )
        )
    return findings


def run_preflight(
    component: str = "all", *, allow_unsigned_preview: bool = False
) -> list[Finding]:
    findings: list[Finding] = []
    if component in {"desktop", "all"}:
        findings.extend(desktop_findings(allow_unsigned_preview=allow_unsigned_preview))
    if component in {"cloud", "all"}:
        findings.extend(cloud_findings())
    return findings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=("desktop", "cloud", "all"), nargs="?", default="all")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--allow-unsigned-preview",
        action="store_true",
        help="treat missing Apple signing and notarization credentials as warnings",
    )
    args = parser.parse_args(argv)
    findings = run_preflight(
        args.component,
        allow_unsigned_preview=args.allow_unsigned_preview,
    )
    if args.json:
        print(json.dumps([asdict(item) for item in findings], ensure_ascii=False, indent=2))
    else:
        for item in findings:
            print(f"[{item.status.upper():7}] {item.component:7} {item.name}: {item.detail}")
    if any(item.blocking for item in findings):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
