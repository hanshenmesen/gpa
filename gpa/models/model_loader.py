"""Model downloading and caching utilities."""
from __future__ import annotations

import logging
from pathlib import Path

from gpa.config import MODELS_CACHE_DIR, HF_GUI_DETECTOR, HF_ICON_CLIP, HF_SENTENCE_E5

logger = logging.getLogger(__name__)


def ensure_all_models() -> None:
    """Pre-download all required models (called on first use or via CLI)."""
    logger.info("Ensuring all models are downloaded …")
    _ensure_gui_detector()
    _ensure_icon_clip()
    _ensure_sentence_e5()
    logger.info("All models ready.")


def _ensure_gui_detector() -> Path:
    model_path = MODELS_CACHE_DIR / "gpa_gui_detector.pt"
    if model_path.exists():
        return model_path
    from huggingface_hub import hf_hub_download
    import shutil
    logger.info(f"Downloading {HF_GUI_DETECTOR} …")
    downloaded = hf_hub_download(
        repo_id=HF_GUI_DETECTOR, filename="model.pt",
        cache_dir=str(MODELS_CACHE_DIR),
    )
    shutil.copy(downloaded, model_path)
    logger.info(f"GPA-GUI-Detector saved to {model_path}")
    return model_path


def _ensure_icon_clip() -> None:
    from transformers import CLIPModel, CLIPProcessor
    cache = MODELS_CACHE_DIR / "iconclip"
    if not (cache / "config.json").exists():
        logger.info(f"Downloading {HF_ICON_CLIP} …")
        CLIPProcessor.from_pretrained(HF_ICON_CLIP, cache_dir=str(MODELS_CACHE_DIR))
        CLIPModel.from_pretrained(HF_ICON_CLIP, cache_dir=str(MODELS_CACHE_DIR))


def _ensure_sentence_e5() -> None:
    from sentence_transformers import SentenceTransformer
    cache = MODELS_CACHE_DIR / "sentence_e5"
    if not cache.exists():
        logger.info(f"Downloading {HF_SENTENCE_E5} …")
        SentenceTransformer(HF_SENTENCE_E5, cache_folder=str(MODELS_CACHE_DIR))
