import tempfile
import unittest
import zipfile
from pathlib import Path

from gpa.community.safety import require_safe_workflow_package, scan_workflow_package


class CommunitySafetyTests(unittest.TestCase):
    def package(self, text: str) -> Path:
        root = Path(self._tmp.name)
        path = root / f"fixture-{len(list(root.glob('*.zip')))}.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("workflow/workflow.yaml", text)
        return path

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_scan_redacts_detected_secret(self):
        fake_key = "sk-proj-" + "abcdefghijklmnopqrstuvwxyz123456"
        package = self.package(f"api_key: {fake_key}")
        result = scan_workflow_package(package)

        self.assertFalse(result["passed"])
        self.assertEqual(result["findings"][0]["type"], "openai_api_key")
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", str(result))

    def test_scan_blocks_arbitrary_execution_actions(self):
        package = self.package("steps:\n  - action_type: shell\n    value: whoami\n")
        with self.assertRaisesRegex(ValueError, "dangerous_execution_action"):
            require_safe_workflow_package(package)

    def test_scan_allows_regular_replay_actions(self):
        package = self.package("steps:\n  - action_type: click\n  - action_type: type\n")
        self.assertTrue(require_safe_workflow_package(package)["passed"])


if __name__ == "__main__":
    unittest.main()
