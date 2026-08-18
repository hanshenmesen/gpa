import tempfile
import unittest
from pathlib import Path

from scripts.generate_update_manifest import build_manifest


class UpdateManifestTests(unittest.TestCase):
    def test_manifest_has_immutable_checksum_and_release_url(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "GPA.dmg"
            artifact.write_bytes(b"preview")
            manifest = build_manifest(
                "v0.1.0-preview.4",
                [artifact],
                repository="hanshenmesen/gpa",
                architecture="arm64",
            )
        self.assertEqual(len(manifest["assets"][0]["sha256"]), 64)
        self.assertIn("/v0.1.0-preview.4/GPA.dmg", manifest["assets"][0]["download_url"])
        self.assertEqual(manifest["installation"], "manual_preview")


if __name__ == "__main__":
    unittest.main()
