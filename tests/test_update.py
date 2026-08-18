import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gpa.update import (
    DesktopUpdateService,
    ReleaseAsset,
    ReleaseInfo,
    UpdateCheckError,
    release_key,
    update_available,
)


class DesktopUpdateTests(unittest.TestCase):
    def test_release_order_handles_preview_and_stable_channels(self):
        self.assertLess(release_key("0.1.0-preview.3"), release_key("0.1.0-rc.1"))
        self.assertLess(release_key("0.1.0-rc.1"), release_key("0.1.0"))
        self.assertTrue(update_available("0.1.0-preview.3", "0.1.0-preview.4"))
        self.assertFalse(update_available("0.1.0", "0.1.0-preview.99"))

    def test_check_persists_only_public_release_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "update.json"
            service = DesktopUpdateService(cache_path=path, current_release="0.1.0-preview.3")
            release = ReleaseInfo(
                release="0.1.0-preview.4",
                title="Preview 4",
                page_url="https://github.com/hanshenmesen/gpa/releases/tag/v0.1.0-preview.4",
                published_at="2026-08-18T00:00:00Z",
                prerelease=True,
                assets=(ReleaseAsset("GPA.dmg", "https://github.com/example/GPA.dmg", 123),),
            )
            with patch.object(service, "_fetch_releases", return_value=([release], '"etag"')):
                result = service.check(force=True)
            self.assertTrue(result["update_available"])
            self.assertEqual(result["latest"]["release"], "0.1.0-preview.4")
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("token", json.dumps(persisted).lower())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_network_failure_uses_cached_metadata_without_claiming_freshness(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "update.json"
            path.write_text(json.dumps({
                "checked_at": 1,
                "latest": {"release": "0.1.0-preview.4", "page_url": "https://github.com/example"},
            }), encoding="utf-8")
            service = DesktopUpdateService(cache_path=path, current_release="0.1.0-preview.3")
            with patch.object(
                service,
                "_fetch_releases",
                side_effect=UpdateCheckError("Could not reach the update service."),
            ):
                result = service.check(force=True)
            self.assertTrue(result["cached"])
            self.assertTrue(result["update_available"])
            self.assertIn("Could not reach", result["error"])


if __name__ == "__main__":
    unittest.main()
