"""UI Parser: extract UI elements from screenshots.

Pipeline:
  1. YOLO (Salesforce/GPA-GUI-Detector) → icon bounding boxes
  2. OCR (ocrmac on macOS, easyocr fallback) → text bounding boxes
  3. IconCLIP (ViT-B-32) → 512-d visual embeddings for all elements
  4. Sentence-E5 (multilingual-e5-small) → 384-d text embeddings for text elements
  5. KNN graph construction
"""
from __future__ import annotations

import copy
import hashlib
import logging
import math
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PIL import Image

from gpa.config import (
    HF_GUI_DETECTOR,
    HF_ICON_CLIP,
    HF_SENTENCE_E5,
    KNN_K,
    MODELS_CACHE_DIR,
    UI_PARSE_CACHE_SIZE,
    UI_PARSER_BACKEND,
    YOLO_CONF,
    YOLO_IMGSZ,
    YOLO_IOU,
)
from gpa.core.ui_graph import UIGraph, UINode

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────── #
# Lazy model holders                                                           #
# ──────────────────────────────────────────────────────────────────────────── #

_yolo_model = None
_clip_model = None
_clip_processor = None
_e5_model = None
_ocr_engine = None   # ocrmac or easyocr
_parse_cache: OrderedDict[tuple, UIGraph] = OrderedDict()
_parse_cache_lock = threading.Lock()
_parser_backends: dict[str, Callable[..., UIGraph]] = {}
_warned_once: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key in _warned_once:
        logger.debug(message)
        return
    _warned_once.add(key)
    logger.warning(message)


def register_parser_backend(name: str, parser: Callable[..., UIGraph]) -> None:
    """Register an experimental parser backend.

    Parser callables receive the same keyword arguments as `parse_screenshot`
    except `backend` and `use_cache`, and must return a `UIGraph`.
    """
    backend_name = str(name or "").strip().casefold()
    if not backend_name:
        raise ValueError("Parser backend name cannot be empty.")
    if backend_name == "builtin":
        raise ValueError("The builtin parser backend cannot be overridden.")
    _parser_backends[backend_name] = parser


def list_parser_backends() -> list[str]:
    return ["builtin", *sorted(_parser_backends)]


def _resolve_parser_backend(name: Optional[str]) -> tuple[str, Callable[..., UIGraph]]:
    backend_name = str(name or UI_PARSER_BACKEND or "builtin").strip().casefold()
    if backend_name == "builtin":
        return backend_name, _parse_screenshot_builtin
    parser = _parser_backends.get(backend_name)
    if parser is None:
        raise ValueError(
            f"Unknown UI parser backend: {backend_name}. "
            f"Available backends: {', '.join(list_parser_backends())}."
        )
    return backend_name, parser


def clear_parse_cache() -> None:
    """Clear the in-process screenshot parse cache."""
    with _parse_cache_lock:
        _parse_cache.clear()


def _image_cache_key(
    image: Image.Image,
    window_bounds: Optional[list[float]],
    knn_k: int,
    scale_factor: float,
    backend_name: str,
) -> tuple:
    digest = hashlib.blake2b(digest_size=16)
    digest.update(str(image.mode).encode("utf-8"))
    digest.update(str(image.size).encode("utf-8"))
    digest.update(image.tobytes())
    bounds_key = tuple(round(float(v), 3) for v in window_bounds) if window_bounds else None
    return (
        digest.hexdigest(),
        image.size,
        image.mode,
        bounds_key,
        int(knn_k),
        round(float(scale_factor), 4),
        backend_name,
    )


def _cache_get(key: tuple) -> Optional[UIGraph]:
    if UI_PARSE_CACHE_SIZE <= 0:
        return None
    with _parse_cache_lock:
        graph = _parse_cache.get(key)
        if graph is None:
            return None
        _parse_cache.move_to_end(key)
        return copy.deepcopy(graph)


def _cache_put(key: tuple, graph: UIGraph) -> None:
    if UI_PARSE_CACHE_SIZE <= 0:
        return
    with _parse_cache_lock:
        _parse_cache[key] = copy.deepcopy(graph)
        _parse_cache.move_to_end(key)
        while len(_parse_cache) > UI_PARSE_CACHE_SIZE:
            _parse_cache.popitem(last=False)


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000.0, 3)


def _load_yolo():
    global _yolo_model
    if _yolo_model is None:
        from huggingface_hub import hf_hub_download
        from ultralytics import YOLO
        model_path = MODELS_CACHE_DIR / "gpa_gui_detector.pt"
        if not model_path.exists():
            logger.info("Downloading GPA-GUI-Detector from HuggingFace …")
            downloaded = hf_hub_download(
                repo_id=HF_GUI_DETECTOR, filename="model.pt",
                cache_dir=str(MODELS_CACHE_DIR),
            )
            import shutil
            shutil.copy(downloaded, model_path)
        _yolo_model = YOLO(str(model_path))
        logger.info("GPA-GUI-Detector loaded.")
    return _yolo_model


def _load_clip():
    global _clip_model, _clip_processor
    if _clip_model is None:
        from transformers import CLIPModel, CLIPProcessor
        logger.info("Loading IconCLIP (openai/clip-vit-base-patch32) …")
        try:
            _clip_processor = CLIPProcessor.from_pretrained(
                HF_ICON_CLIP, cache_dir=str(MODELS_CACHE_DIR), local_files_only=True
            )
            _clip_model = CLIPModel.from_pretrained(
                HF_ICON_CLIP, cache_dir=str(MODELS_CACHE_DIR), local_files_only=True
            )
        except Exception as local_exc:
            logger.info("Local IconCLIP cache unavailable; falling back to HuggingFace: %s", local_exc)
            _clip_processor = CLIPProcessor.from_pretrained(
                HF_ICON_CLIP, cache_dir=str(MODELS_CACHE_DIR)
            )
            _clip_model = CLIPModel.from_pretrained(
                HF_ICON_CLIP, cache_dir=str(MODELS_CACHE_DIR)
            )
        _clip_model.eval()
        logger.info("IconCLIP loaded.")
    return _clip_model, _clip_processor


def _load_e5():
    global _e5_model
    if _e5_model is None:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading Sentence-E5 …")
        try:
            _e5_model = SentenceTransformer(
                HF_SENTENCE_E5,
                cache_folder=str(MODELS_CACHE_DIR),
                local_files_only=True,
            )
        except Exception as local_exc:
            logger.info("Local Sentence-E5 cache unavailable; falling back to HuggingFace: %s", local_exc)
            _e5_model = SentenceTransformer(HF_SENTENCE_E5, cache_folder=str(MODELS_CACHE_DIR))
        logger.info("Sentence-E5 loaded.")
    return _e5_model


def _load_ocr():
    global _ocr_engine
    if _ocr_engine is None:
        try:
            from ocrmac.ocrmac import OCR  # noqa: F401
            _ocr_engine = "ocrmac"
            logger.info("OCR backend: ocrmac (Apple Vision)")
        except ImportError:
            try:
                import easyocr
                _ocr_engine = easyocr.Reader(["en", "ch_sim"], gpu=False)
                logger.info("OCR backend: easyocr")
            except ImportError:
                logger.warning("No OCR backend available. Text detection disabled.")
                _ocr_engine = "none"
    return _ocr_engine


# ──────────────────────────────────────────────────────────────────────────── #
# Detection helpers                                                            #
# ──────────────────────────────────────────────────────────────────────────── #

def _detect_icons(image: Image.Image) -> list[dict]:
    """Run YOLO detector, return list of {pos:[x,y,w,h], conf:float}."""
    model = _load_yolo()
    results = model.predict(
        source=image,
        conf=YOLO_CONF,
        imgsz=YOLO_IMGSZ,
        iou=YOLO_IOU,
        verbose=False,
    )
    detections = []
    for r in results:
        boxes = r.boxes.xyxy.cpu().numpy()   # [x1,y1,x2,y2]
        confs = r.boxes.conf.cpu().numpy()
        for box, conf in zip(boxes, confs, strict=True):
            x1, y1, x2, y2 = box
            detections.append({
                "pos": [float(x1), float(y1), float(x2 - x1), float(y2 - y1)],
                "conf": float(conf),
            })
    return detections


def _detect_text_ocrmac(image: Image.Image) -> list[dict]:
    """Use ocrmac (Apple Vision) for text detection. Accepts PIL image directly."""
    from ocrmac.ocrmac import OCR
    results = OCR(image, recognition_level="accurate").recognize()
    texts = []
    iw, ih = image.size
    for item in results:
        # item: (text, confidence, [x, y, w, h]) normalized, y from bottom-left
        if not (isinstance(item, (list, tuple)) and len(item) >= 3):
            continue
        text, conf, bbox = item[0], item[1], item[2]
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            nx_, ny, nw, nh = bbox
            # Convert from normalized bottom-left to pixel top-left
            px = nx_ * iw
            py = (1 - ny - nh) * ih
            pw = nw * iw
            ph = nh * ih
            if pw > 2 and ph > 2 and text.strip():
                texts.append({
                    "pos": [px, py, pw, ph],
                    "content": text.strip(),
                    "conf": float(conf),
                })
    return texts


def _detect_text_easyocr(image: Image.Image) -> list[dict]:
    """Use easyocr for text detection."""
    reader = _load_ocr()
    arr = np.array(image)
    results = reader.readtext(arr)
    texts = []
    for bbox_pts, text, conf in results:
        # bbox_pts: [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]
        xs = [p[0] for p in bbox_pts]
        ys = [p[1] for p in bbox_pts]
        x, y = min(xs), min(ys)
        w, h = max(xs) - x, max(ys) - y
        texts.append({"pos": [x, y, w, h], "content": text, "conf": conf})
    return texts


def _detect_text(image: Image.Image) -> list[dict]:
    ocr = _load_ocr()
    if ocr == "ocrmac":
        try:
            return _detect_text_ocrmac(image)
        except Exception as e:
            logger.warning(f"ocrmac failed: {e}, falling back to easyocr")
            try:
                import easyocr
                global _ocr_engine
                _ocr_engine = easyocr.Reader(["en", "ch_sim"], gpu=False)
                return _detect_text_easyocr(image)
            except ImportError:
                return []
    elif ocr == "none" or ocr is None:
        return []
    else:
        # easyocr reader object
        return _detect_text_easyocr(image)


# ──────────────────────────────────────────────────────────────────────────── #
# Embedding helpers                                                            #
# ──────────────────────────────────────────────────────────────────────────── #

def _compute_icon_embeddings(image: Image.Image, boxes: list[list[float]]) -> np.ndarray:
    """Crop each box from image, compute IconCLIP embeddings. Returns (N, 512)."""
    if not boxes:
        return np.zeros((0, 512), dtype=np.float32)

    model, processor = _load_clip()
    import torch

    crops = []
    for pos in boxes:
        x, y, w, h = pos
        x1, y1 = max(0, int(x)), max(0, int(y))
        x2, y2 = min(image.width, int(x + w)), min(image.height, int(y + h))
        if x2 <= x1 or y2 <= y1:
            crops.append(Image.new("RGB", (32, 32)))
        else:
            crops.append(image.crop((x1, y1, x2, y2)).convert("RGB"))

    inputs = processor(images=crops, return_tensors="pt", padding=True)
    with torch.no_grad():
        # Use vision_model + visual_projection to get projected 512-d embeddings
        vision_out = model.vision_model(**{k: v for k, v in inputs.items() if k == "pixel_values"})
        # CLS token (index 0) → project to embedding space
        cls_feats = vision_out.last_hidden_state[:, 0, :]  # (N, hidden_dim)
        if hasattr(model, "visual_projection"):
            feats = model.visual_projection(cls_feats)      # (N, 512)
        else:
            feats = cls_feats
        feats = feats / feats.norm(dim=-1, keepdim=True).clamp_min(1e-12)
    return feats.cpu().numpy().astype(np.float32)


def _compute_text_embeddings(texts: list[Optional[str]]) -> np.ndarray:
    """Compute Sentence-E5 embeddings for text content. Returns (N, 384)."""
    valid = [(i, t) for i, t in enumerate(texts) if t]
    if not valid:
        return np.zeros((len(texts), 384), dtype=np.float32)

    model = _load_e5()
    indices, sentences = zip(*valid, strict=True)
    # E5 models work best with "query: " prefix for retrieval
    prefixed = [f"passage: {s}" for s in sentences]
    embs = model.encode(list(prefixed), normalize_embeddings=True, show_progress_bar=False)

    result = np.zeros((len(texts), embs.shape[1]), dtype=np.float32)
    for i, idx in enumerate(indices):
        result[idx] = embs[i]
    return result


# ──────────────────────────────────────────────────────────────────────────── #
# NMS / deduplication                                                          #
# ──────────────────────────────────────────────────────────────────────────── #

def _iou(a: list[float], b: list[float]) -> float:
    ax1, ay1, aw, ah = a
    bx1, by1, bw, bh = b
    ax2, ay2 = ax1 + aw, ay1 + ah
    bx2, by2 = bx1 + bw, by1 + bh
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _remove_overlaps(icon_boxes, text_boxes, iou_thr=0.5):
    """Remove icon detections that heavily overlap with text regions."""
    keep_icons = []
    for ib in icon_boxes:
        overlap = any(_iou(ib["pos"], tb["pos"]) > iou_thr for tb in text_boxes)
        if not overlap:
            keep_icons.append(ib)
    return keep_icons


# ──────────────────────────────────────────────────────────────────────────── #
# Public API                                                                   #
# ──────────────────────────────────────────────────────────────────────────── #

def parse_screenshot(
    image: Image.Image,
    window_bounds: Optional[list[float]] = None,
    knn_k: int = KNN_K,
    scale_factor: float = 1.0,
    use_cache: bool = True,
    backend: Optional[str] = None,
) -> UIGraph:
    """Full pipeline: screenshot → UIGraph with embeddings + KNN edges.

    Args:
        image: PIL Image of the current screen (or window crop).
        window_bounds: [x, y, w, h] of the app window in screen coordinates.
        knn_k: number of neighbours per node.
        scale_factor: screen scale (2.0 for retina).
        use_cache: reuse identical screenshot parses within this process.
        backend: parser backend name; defaults to GPA_UI_PARSER_BACKEND.
    """
    if not isinstance(image, Image.Image):
        raise TypeError("image must be a PIL.Image.Image")
    if not isinstance(knn_k, int) or isinstance(knn_k, bool) or knn_k < 0:
        raise ValueError("knn_k must be a non-negative integer")
    if not math.isfinite(float(scale_factor)) or scale_factor <= 0:
        raise ValueError("scale_factor must be finite and greater than zero")
    if window_bounds is not None:
        if len(window_bounds) != 4 or not all(math.isfinite(float(value)) for value in window_bounds):
            raise ValueError("window_bounds must contain four finite numbers")

    total_start = time.perf_counter()
    backend_name, parser = _resolve_parser_backend(backend)
    cache_key = None
    cache_key_ms = 0.0
    if use_cache:
        try:
            cache_start = time.perf_counter()
            cache_key = _image_cache_key(image, window_bounds, knn_k, scale_factor, backend_name)
            cache_key_ms = _elapsed_ms(cache_start)
            cached = _cache_get(cache_key)
            if cached is not None:
                logger.debug("UIGraph parse cache hit.")
                cached.parse_metrics = {
                    **getattr(cached, "parse_metrics", {}),
                    "backend": backend_name,
                    "cache_hit": True,
                    "cache_key_ms": cache_key_ms,
                    "total_ms": _elapsed_ms(total_start),
                }
                return cached
        except Exception as e:
            logger.debug("UIGraph parse cache key failed: %s", e)
            cache_key = None

    graph = parser(
        image,
        window_bounds=window_bounds,
        knn_k=knn_k,
        scale_factor=scale_factor,
    )
    if not isinstance(graph, UIGraph):
        raise TypeError(f"UI parser backend {backend_name!r} must return a UIGraph")
    graph.parse_metrics = {
        **getattr(graph, "parse_metrics", {}),
        "backend": backend_name,
        "cache_hit": False,
        "cache_key_ms": cache_key_ms,
        "total_ms": _elapsed_ms(total_start),
    }
    if cache_key is not None:
        _cache_put(cache_key, graph)
    return graph


def _parse_screenshot_builtin(
    image: Image.Image,
    window_bounds: Optional[list[float]] = None,
    knn_k: int = KNN_K,
    scale_factor: float = 1.0,
) -> UIGraph:
    metrics: dict[str, object] = {
        "backend": "builtin",
        "scale_factor": float(scale_factor),
        "image_width": int(image.width),
        "image_height": int(image.height),
    }

    # 1. Detect icons and text
    logger.debug("Detecting icons with YOLO …")
    started = time.perf_counter()
    try:
        icon_dets = _detect_icons(image)
    except Exception as e:
        _warn_once(
            "icon_detection_unavailable",
            f"Icon detection unavailable: {e}. Continuing without icon nodes.",
        )
        icon_dets = []
    metrics["detect_icons_ms"] = _elapsed_ms(started)
    metrics["icon_count"] = len(icon_dets)

    logger.debug("Detecting text with OCR …")
    started = time.perf_counter()
    try:
        text_dets = _detect_text(image)
    except Exception as e:
        _warn_once("ocr_unavailable", f"OCR unavailable: {e}. Continuing without text nodes.")
        text_dets = []
    metrics["ocr_ms"] = _elapsed_ms(started)
    metrics["text_count"] = len(text_dets)

    # 2. Remove icon detections that are actually text
    started = time.perf_counter()
    icon_dets = _remove_overlaps(icon_dets, text_dets)
    metrics["dedupe_ms"] = _elapsed_ms(started)
    metrics["icon_count_after_dedupe"] = len(icon_dets)

    # 3. Build node list
    started = time.perf_counter()
    all_nodes: list[UINode] = []
    all_boxes: list[list[float]] = []

    nid = 0
    for det in icon_dets:
        all_nodes.append(UINode(
            id=nid, pos=det["pos"], elem_type="icon", content=None,
        ))
        all_boxes.append(det["pos"])
        nid += 1
    for det in text_dets:
        all_nodes.append(UINode(
            id=nid, pos=det["pos"], elem_type="text", content=det.get("content"),
        ))
        all_boxes.append(det["pos"])
        nid += 1
    metrics["build_nodes_ms"] = _elapsed_ms(started)
    metrics["node_count"] = len(all_nodes)

    if not all_nodes:
        _warn_once("no_ui_elements_detected", "No UI elements detected in screenshot.")
        graph = UIGraph(image_size=[image.width, image.height], window_bounds=window_bounds)
        graph.parse_metrics = metrics
        return graph

    # 4. Compute IconCLIP embeddings for ALL nodes
    logger.debug(f"Computing IconCLIP embeddings for {len(all_nodes)} nodes …")
    started = time.perf_counter()
    try:
        icon_embs = _compute_icon_embeddings(image, all_boxes)
    except Exception as e:
        _warn_once("icon_embeddings_unavailable", f"Icon embeddings unavailable: {e}. Using zero embeddings.")
        icon_embs = np.zeros((len(all_nodes), 512), dtype=np.float32)
    metrics["icon_embedding_ms"] = _elapsed_ms(started)
    for i, node in enumerate(all_nodes):
        node.icon_emb = icon_embs[i]

    # 5. Compute Sentence-E5 embeddings for text nodes
    texts = [n.content for n in all_nodes]
    has_text = any(t is not None for t in texts)
    if has_text:
        logger.debug("Computing Sentence-E5 embeddings …")
        started = time.perf_counter()
        try:
            text_embs = _compute_text_embeddings(texts)
        except Exception as e:
            _warn_once("text_embeddings_unavailable", f"Text embeddings unavailable: {e}. Using zero embeddings.")
            text_embs = np.zeros((len(texts), 384), dtype=np.float32)
        metrics["text_embedding_ms"] = _elapsed_ms(started)
        for i, node in enumerate(all_nodes):
            if node.content is not None:
                node.text_emb = text_embs[i]
    else:
        metrics["text_embedding_ms"] = 0.0

    # 6. Build graph
    started = time.perf_counter()
    graph = UIGraph(
        nodes=all_nodes,
        image_size=[image.width, image.height],
        window_bounds=window_bounds,
    )
    graph.build_knn_edges(k=knn_k)
    metrics["graph_build_ms"] = _elapsed_ms(started)
    metrics["edge_count"] = len(graph.edges)
    graph.parse_metrics = metrics
    logger.debug(f"UIGraph: {len(all_nodes)} nodes, {len(graph.edges)} edges.")
    return graph


def parse_screenshot_path(path: str | Path, **kwargs) -> UIGraph:
    with Image.open(path) as source:
        img = source.convert("RGB")
    return parse_screenshot(img, **kwargs)
