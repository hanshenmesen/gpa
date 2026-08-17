"""Model downloading and caching utilities."""
from __future__ import annotations

import logging
from pathlib import Path

from gpa.config import HF_GUI_DETECTOR, HF_ICON_CLIP, HF_SENTENCE_E5, MODELS_CACHE_DIR

logger = logging.getLogger(__name__)


class ModelDependencyError(RuntimeError):
    """Raised when optional visual model tooling is not installed."""


def _huggingface_downloaders():
    try:
        from huggingface_hub import hf_hub_download, snapshot_download
    except ImportError as exc:
        raise ModelDependencyError(
            "Visual model downloads require the 'visual' dependencies. "
            "Install them with: pip install 'gpa[visual]'"
        ) from exc
    return hf_hub_download, snapshot_download


def ensure_all_models() -> dict[str, Path]:
    """Pre-download all required models (called on first use or via CLI)."""
    logger.info("Ensuring all models are downloaded …")
    paths = {
        "gui_detector": _ensure_gui_detector(),
        "icon_clip": _ensure_icon_clip(),
        "sentence_e5": _ensure_sentence_e5(),
    }
    logger.info("All models ready.")
    return paths


def _ensure_gui_detector() -> Path:
    MODELS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    model_path = MODELS_CACHE_DIR / "gpa_gui_detector.pt"
    if model_path.exists():
        return model_path
    import shutil

    hf_hub_download, _ = _huggingface_downloaders()

    logger.info(f"Downloading {HF_GUI_DETECTOR} …")
    downloaded = hf_hub_download(
        repo_id=HF_GUI_DETECTOR, filename="model.pt",
        cache_dir=str(MODELS_CACHE_DIR),
    )
    shutil.copy(downloaded, model_path)
    logger.info(f"GPA-GUI-Detector saved to {model_path}")
    return model_path


def _ensure_icon_clip() -> Path:
    _, snapshot_download = _huggingface_downloaders()
    logger.info(f"Ensuring {HF_ICON_CLIP} is cached …")
    return Path(snapshot_download(repo_id=HF_ICON_CLIP, cache_dir=str(MODELS_CACHE_DIR)))


def _ensure_sentence_e5() -> Path:
    _, snapshot_download = _huggingface_downloaders()
    logger.info(f"Ensuring {HF_SENTENCE_E5} is cached …")
    return Path(snapshot_download(repo_id=HF_SENTENCE_E5, cache_dir=str(MODELS_CACHE_DIR)))


__all__ = ["ModelDependencyError", "ensure_all_models"]
