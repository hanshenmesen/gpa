"""Run GPA as a native window backed by the loopback-only application service."""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable

from gpa import __version__
from gpa.runtime_config import env_bool, user_data_path


class DesktopRuntimeError(RuntimeError):
    """Raised when the native desktop shell cannot start safely."""


@dataclass(frozen=True)
class DesktopAppConfig:
    title: str = "GPA"
    width: int = 1440
    height: int = 920
    minimum_width: int = 1040
    minimum_height: int = 700
    port: int = 0
    debug: bool = False
    persistent_session: bool = True

    @classmethod
    def from_environment(
        cls,
        *,
        port: int = 0,
        debug: bool | None = None,
        persistent_session: bool = True,
    ) -> "DesktopAppConfig":
        resolved_debug = env_bool("GPA_DESKTOP_DEBUG", False) if debug is None else bool(debug)
        return cls(
            port=_validated_port(port),
            debug=resolved_debug,
            persistent_session=bool(persistent_session),
        )

    @property
    def storage_path(self) -> Path:
        return user_data_path("GPA") / "webview"


class LocalConsoleRuntime:
    """Own the local HTTP service for exactly one desktop window lifetime."""

    def __init__(self, *, port: int = 0) -> None:
        self.port = _validated_port(port)
        self.server = None
        self.url = ""

    def start(self) -> str:
        if self.server is not None:
            return self.url
        from demo_web import server as local_server

        server = local_server.start_server(port=self.port)
        bound_host, bound_port = server.server_address[:2]
        if bound_host not in {"127.0.0.1", "::1"}:
            local_server.stop_server(server)
            raise DesktopRuntimeError("GPA desktop service must bind to loopback only.")
        self.server = server
        self.url = f"http://127.0.0.1:{int(bound_port)}/"
        return self.url

    def stop(self, *_args) -> None:
        server, self.server = self.server, None
        if server is None:
            return
        from demo_web import server as local_server

        local_server.stop_server(server)


def _validated_port(port: int) -> int:
    value = int(port)
    if value != 0 and not 1024 <= value <= 65535:
        raise DesktopRuntimeError("Desktop port must be 0 or between 1024 and 65535.")
    return value


def _load_webview() -> ModuleType:
    try:
        import webview
    except ImportError as exc:
        raise DesktopRuntimeError(
            "Desktop support is not installed. Run: python -m pip install -e '.[desktop]'"
        ) from exc
    return webview


def launch_desktop(
    config: DesktopAppConfig | None = None,
    *,
    webview_module: ModuleType | None = None,
    runtime_factory: Callable[..., LocalConsoleRuntime] = LocalConsoleRuntime,
) -> int:
    """Launch one native GPA window without exposing a general JS-to-Python bridge."""
    app_config = config or DesktopAppConfig.from_environment()
    webview = webview_module or _load_webview()
    runtime = runtime_factory(port=app_config.port)
    local_url = runtime.start()
    app_config.storage_path.mkdir(parents=True, exist_ok=True)

    settings = getattr(webview, "settings", None)
    if isinstance(settings, dict):
        settings["ALLOW_DOWNLOADS"] = False
        settings["ALLOW_FILE_URLS"] = False
        settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        settings["OPEN_DEVTOOLS_IN_DEBUG"] = bool(app_config.debug)
        settings["REMOTE_DEBUGGING_PORT"] = None

    window = webview.create_window(
        app_config.title,
        url=local_url,
        width=app_config.width,
        height=app_config.height,
        min_size=(app_config.minimum_width, app_config.minimum_height),
        background_color="#F7F8FC",
        text_select=True,
        zoomable=True,
    )
    window.events.closed += runtime.stop
    try:
        webview.start(
            debug=app_config.debug,
            private_mode=not app_config.persistent_session,
            storage_path=str(app_config.storage_path),
            user_agent=f"GPA-Desktop/{__version__}",
        )
    finally:
        runtime.stop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gpa-desktop",
        description="Run GPA in a native desktop window.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="local loopback port; 0 selects an available ephemeral port",
    )
    parser.add_argument("--debug", action="store_true", help="enable WebView developer tools")
    parser.add_argument(
        "--private-session",
        action="store_true",
        help="discard WebView cookies and browser storage when the app closes",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the desktop dependency without opening a window",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        config = DesktopAppConfig.from_environment(
            port=args.port,
            debug=args.debug or env_bool("GPA_DESKTOP_DEBUG", False),
            persistent_session=not args.private_session,
        )
        if args.check:
            _load_webview()
            print("GPA desktop shell is ready.")
            return
        raise SystemExit(launch_desktop(config))
    except DesktopRuntimeError as exc:
        print(f"GPA desktop failed to start: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
