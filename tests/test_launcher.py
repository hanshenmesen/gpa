import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def run_check(self, *arguments, env_content=""):
        environment = dict(os.environ)
        # Prove that safe defaults override a permissive parent shell.
        environment["GPA_ENABLE_DESKTOP_AUTOMATION"] = "1"
        environment["PATH"] = os.pathsep.join(
            [str(Path(sys.executable).parent), environment.get("PATH", "")]
        )
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text(env_content, encoding="utf-8")
            environment["GPA_ENV_FILE"] = str(env_file)
            environment["GPA_VENV_DIR"] = str(Path(temporary) / "missing-venv")
            return subprocess.run(
                ["bash", str(ROOT / "start.sh"), "--check", "--skip-install", *arguments],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

    def test_default_check_is_safe_and_lightweight(self):
        result = self.run_check()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Desktop automation: disabled", result.stdout)
        self.assertIn("Recording input backend: quartz", result.stdout)
        self.assertIn("Visual model preload: disabled", result.stdout)
        self.assertIn("server was not started", result.stdout)

    def test_desktop_opt_in_is_explicit(self):
        result = self.run_check("--enable-desktop")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Desktop automation: ENABLED", result.stdout)

    def test_saved_desktop_preference_is_loaded_from_project_env(self):
        result = self.run_check(env_content="GPA_DESKTOP_STARTUP_ENABLED=1\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("saved device preference", result.stdout)

    def test_web_command_exposes_help_without_starting_server(self):
        result = subprocess.run(
            [sys.executable, "-m", "demo_web.server", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Run GPA's local Web console", result.stdout)
        self.assertIn("--port", result.stdout)


if __name__ == "__main__":
    unittest.main()
