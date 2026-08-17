import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gpa.desktop.app import (
    DesktopAppConfig,
    DesktopRuntimeError,
    LocalConsoleRuntime,
    launch_desktop,
)


class _Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class _Window:
    def __init__(self):
        self.events = type("Events", (), {"closed": _Event()})()


class _WebView:
    def __init__(self):
        self.settings = {}
        self.created = None
        self.started = None
        self.window = _Window()

    def create_window(self, title, **options):
        self.created = (title, options)
        return self.window

    def start(self, **options):
        self.started = options


class _Runtime:
    instances = []

    def __init__(self, *, port=0):
        self.port = port
        self.stop_count = 0
        self.__class__.instances.append(self)

    def start(self):
        return "http://127.0.0.1:43125/"

    def stop(self, *_args):
        self.stop_count += 1


class DesktopAppTests(unittest.TestCase):
    def setUp(self):
        _Runtime.instances.clear()

    def test_desktop_window_is_local_and_hardened(self):
        fake_webview = _WebView()
        with tempfile.TemporaryDirectory() as temporary, patch(
            "gpa.desktop.app.user_data_path", return_value=Path(temporary)
        ):
            result = launch_desktop(
                DesktopAppConfig(port=0, debug=False),
                webview_module=fake_webview,
                runtime_factory=_Runtime,
            )

        self.assertEqual(result, 0)
        title, options = fake_webview.created
        self.assertEqual(title, "GPA")
        self.assertEqual(options["url"], "http://127.0.0.1:43125/")
        self.assertFalse(fake_webview.settings["ALLOW_DOWNLOADS"])
        self.assertFalse(fake_webview.settings["ALLOW_FILE_URLS"])
        self.assertIsNone(fake_webview.settings["REMOTE_DEBUGGING_PORT"])
        self.assertFalse(fake_webview.started["private_mode"])
        self.assertEqual(_Runtime.instances[0].stop_count, 1)

    def test_invalid_fixed_ports_fail_before_server_start(self):
        for port in (-1, 80, 70000):
            with self.subTest(port=port), self.assertRaises(DesktopRuntimeError):
                DesktopAppConfig.from_environment(port=port, debug=False)

    def test_local_console_runtime_reuses_one_started_server(self):
        runtime = LocalConsoleRuntime(port=0)
        sentinel = object()
        runtime.server = sentinel
        runtime.url = "http://127.0.0.1:44500/"
        self.assertEqual(runtime.start(), runtime.url)


if __name__ == "__main__":
    unittest.main()
