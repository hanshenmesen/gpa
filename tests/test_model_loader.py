import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from gpa.integration import cli
from gpa.models import model_loader


class ModelLoaderTests(unittest.TestCase):
    def test_missing_visual_download_dependency_has_actionable_error(self):
        with patch.dict(sys.modules, {"huggingface_hub": None}):
            with self.assertRaisesRegex(model_loader.ModelDependencyError, r"gpa\[visual\]"):
                model_loader._huggingface_downloaders()

    def test_ensure_models_uses_shared_huggingface_cache_without_loading_models(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir) / "models"
            source = Path(temp_dir) / "downloaded-model.pt"
            source.write_bytes(b"model")
            calls = []

            def fake_hf_hub_download(**kwargs):
                calls.append(("file", kwargs))
                return str(source)

            def fake_snapshot_download(**kwargs):
                calls.append(("snapshot", kwargs))
                snapshot = cache / ("snapshot-" + kwargs["repo_id"].replace("/", "--"))
                snapshot.mkdir(parents=True, exist_ok=True)
                return str(snapshot)

            fake_hub = types.ModuleType("huggingface_hub")
            fake_hub.hf_hub_download = fake_hf_hub_download
            fake_hub.snapshot_download = fake_snapshot_download

            with (
                patch.dict(sys.modules, {"huggingface_hub": fake_hub}),
                patch.object(model_loader, "MODELS_CACHE_DIR", cache),
            ):
                paths = model_loader.ensure_all_models()

            self.assertEqual(paths["gui_detector"].read_bytes(), b"model")
            self.assertTrue(paths["icon_clip"].is_dir())
            self.assertTrue(paths["sentence_e5"].is_dir())
            self.assertEqual([kind for kind, _ in calls], ["file", "snapshot", "snapshot"])
            for _, kwargs in calls:
                self.assertEqual(kwargs["cache_dir"], str(cache))

    def test_existing_detector_does_not_call_hub(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir)
            detector = cache / "gpa_gui_detector.pt"
            detector.write_bytes(b"cached")
            with (
                patch.object(model_loader, "MODELS_CACHE_DIR", cache),
                patch.object(model_loader, "_huggingface_downloaders") as downloaders,
            ):
                result = model_loader._ensure_gui_detector()

            self.assertEqual(result, detector)
            downloaders.assert_not_called()

    def test_cli_turns_model_dependency_failure_into_clean_error(self):
        with patch.object(
            model_loader,
            "ensure_all_models",
            side_effect=model_loader.ModelDependencyError("install visual extras"),
        ):
            result = CliRunner().invoke(cli.main, ["download-models"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("install visual extras", result.output)
        self.assertNotIn("Traceback", result.output)


if __name__ == "__main__":
    unittest.main()
