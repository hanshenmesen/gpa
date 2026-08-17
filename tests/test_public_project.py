import tempfile
import unittest
from pathlib import Path

from scripts.verify_public_project import validate_public_project


class PublicProjectTests(unittest.TestCase):
    def test_current_public_project_metadata_is_complete(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(validate_public_project(root), [])

    def test_personal_path_and_broken_link_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in (
                "README.md",
                "CONTRIBUTING.md",
                "SECURITY.md",
                "SUPPORT.md",
                "GOVERNANCE.md",
                "ROADMAP.md",
                "CHANGELOG.md",
                "docs/feedback_program.md",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("[missing](missing.md) /Users/example/private\n", encoding="utf-8")
            errors = validate_public_project(root)
        self.assertTrue(any("personal absolute path" in error for error in errors))
        self.assertTrue(any("broken local link" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
