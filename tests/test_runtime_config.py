import stat
import tempfile
import unittest
from pathlib import Path

from gpa.runtime_config import (
    RuntimeConfigurationError,
    env_bool,
    env_float,
    env_int,
    env_path,
    update_env_file,
    user_data_path,
)


class RuntimeConfigTests(unittest.TestCase):
    def test_boolean_values_are_explicit_and_case_insensitive(self):
        for value in ("1", "true", "YES", "On"):
            self.assertTrue(env_bool("FLAG", environ={"FLAG": value}))
        for value in ("0", "false", "NO", "off"):
            self.assertFalse(env_bool("FLAG", environ={"FLAG": value}))
        self.assertTrue(env_bool("FLAG", True, environ={}))

    def test_invalid_boolean_fails_with_variable_name(self):
        with self.assertRaisesRegex(RuntimeConfigurationError, "FLAG"):
            env_bool("FLAG", environ={"FLAG": "sometimes"})

    def test_float_bounds_are_enforced(self):
        self.assertEqual(env_float("TIMEOUT", 45, environ={}), 45.0)
        self.assertEqual(
            env_float("TIMEOUT", 45, minimum=1, environ={"TIMEOUT": "2.5"}),
            2.5,
        )
        with self.assertRaisesRegex(RuntimeConfigurationError, "at least"):
            env_float("TIMEOUT", 45, minimum=1, environ={"TIMEOUT": "0"})
        with self.assertRaisesRegex(RuntimeConfigurationError, "at most"):
            env_float("TIMEOUT", 45, maximum=60, environ={"TIMEOUT": "61"})

    def test_integer_values_and_bounds_are_enforced(self):
        self.assertEqual(env_int("PORT", 8765, environ={}), 8765)
        self.assertEqual(env_int("PORT", 8765, minimum=1024, environ={"PORT": "9000"}), 9000)
        with self.assertRaisesRegex(RuntimeConfigurationError, "integer"):
            env_int("PORT", 8765, environ={"PORT": "9000.5"})
        with self.assertRaisesRegex(RuntimeConfigurationError, "at least"):
            env_int("PORT", 8765, minimum=1024, environ={"PORT": "80"})

    def test_relative_paths_are_resolved_from_the_project_not_cwd(self):
        base = Path("/project")

        self.assertEqual(env_path("DATA", "storage", base=base, environ={}), base / "storage")
        self.assertEqual(
            env_path("DATA", "storage", base=base, environ={"DATA": "var/data"}),
            base / "var/data",
        )
        self.assertEqual(
            env_path("DATA", "storage", base=base, environ={"DATA": "/srv/gpa"}),
            Path("/srv/gpa"),
        )

    def test_user_data_path_follows_platform_conventions(self):
        self.assertEqual(
            user_data_path("GPA", platform_name="darwin", home="/Users/test"),
            Path("/Users/test/Library/Application Support/GPA"),
        )
        self.assertEqual(
            user_data_path(
                "GPA",
                platform_name="linux",
                environ={"XDG_DATA_HOME": "/data/user"},
                home="/home/test",
            ),
            Path("/data/user/GPA"),
        )
        self.assertEqual(
            user_data_path(
                "GPA",
                platform_name="win32",
                environ={"LOCALAPPDATA": "C:/Users/test/AppData/Local"},
                home="C:/Users/test",
            ),
            Path("C:/Users/test/AppData/Local/GPA"),
        )
        with self.assertRaisesRegex(RuntimeConfigurationError, "XDG_DATA_HOME"):
            user_data_path(
                "GPA",
                platform_name="linux",
                environ={"XDG_DATA_HOME": "relative"},
                home="/home/test",
            )

    def test_dotenv_update_is_atomic_private_and_does_not_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            path.write_text("KEEP=yes\nGPA_LLM_API_KEY=old\nexport GPA_LLM_MODEL=old-model\n")

            update_env_file(path, {
                "GPA_LLM_API_KEY": 'new key $HOME `unsafe` \'quoted\'',
                "GPA_LLM_MODEL": "gpt-new",
                "GPA_LLM_TEXT_MODEL": None,
            })

            content = path.read_text()
            self.assertIn("KEEP=yes", content)
            self.assertIn(
                "GPA_LLM_API_KEY='new key $HOME `unsafe` '\"'\"'quoted'\"'\"''",
                content,
            )
            self.assertEqual(content.count("GPA_LLM_API_KEY="), 1)
            self.assertEqual(content.count("GPA_LLM_MODEL="), 1)
            self.assertNotIn("GPA_LLM_TEXT_MODEL", content)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_dotenv_update_rejects_newlines_and_invalid_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            with self.assertRaisesRegex(RuntimeConfigurationError, "control character"):
                update_env_file(path, {"GPA_LLM_API_KEY": "secret\nINJECTED=1"})
            with self.assertRaisesRegex(RuntimeConfigurationError, "variable name"):
                update_env_file(path, {"bad-name": "value"})

    def test_web_cli_and_mcp_share_the_configured_workflow_repository(self):
        import demo_web.server as server
        from gpa.config import WORKFLOWS_DIR

        self.assertEqual(server.WORKFLOWS_DIR, WORKFLOWS_DIR)


if __name__ == "__main__":
    unittest.main()
