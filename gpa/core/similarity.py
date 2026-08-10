"""Node similarity computation.

Implements the hybrid similarity from the paper (Section 2.1, Appendix B.2):
  - Textual nodes:  s = 0.9 * s_text + 0.1 * cos(e_d, e_c)
  - Non-textual:    s = cos(e_d, e_c)

Text similarity uses RapidFuzz Levenshtein ratio with length-adaptive tolerance.
"""
from __future__ import annotations

import numpy as np
from rapidfuzz import fuzz

from gpa.core.ui_graph import UINode


# ──────────────────────────────────────────────────────────────────────────── #
# Primitive similarities                                                       #
# ──────────────────────────────────────────────────────────────────────────── #

def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity in [0, 1]."""
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-8 or nb < 1e-8:
        return 0.0
    return float(np.clip(np.dot(a, b) / (na * nb), -1.0, 1.0) * 0.5 + 0.5)


def text_similarity(a: str, b: str) -> float:
    """Levenshtein ratio with length-adaptive tolerance → [0, 1].

    Short strings (< 4 chars) get extra forgiveness for OCR errors.
    """
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    ratio = fuzz.ratio(a.lower().strip(), b.lower().strip()) / 100.0
    # For very short strings the raw ratio is harsh; soften it slightly
    min_len = min(len(a), len(b))
    if min_len <= 3:
        # Allow a 1-character edit to pass as ~0.8 similarity
        ratio = max(ratio, 0.8 * (1.0 - 1.0 / (min_len + 1)))
    return float(ratio)


# ──────────────────────────────────────────────────────────────────────────── #
# Node-level similarity  (paper Appendix B.2)                                 #
# ──────────────────────────────────────────────────────────────────────────── #

def node_similarity(demo: UINode, runtime: UINode) -> float:
    """Compute appearance similarity w_app(v_d, v_c) ∈ [0, 1].

    For textual elements: 0.9 * s_text + 0.1 * cos(icon_emb)
    For non-textual:      cos(icon_emb)
    """
    # Icon embedding similarity (always available)
    icon_sim = 0.0
    if demo.icon_emb is not None and runtime.icon_emb is not None:
        icon_sim = cosine_similarity(demo.icon_emb, runtime.icon_emb)

    # Text branch
    is_text = (demo.elem_type == "text") and (demo.content is not None)
    if is_text and runtime.content is not None:
        t_sim = text_similarity(demo.content, runtime.content)
        return 0.9 * t_sim + 0.1 * icon_sim
    elif is_text:
        # Demo is text but runtime node has no OCR content → icon only
        return icon_sim

    # Icon-only branch
    return icon_sim


# ──────────────────────────────────────────────────────────────────────────── #
# Batch candidate scoring                                                      #
# ──────────────────────────────────────────────────────────────────────────── #

def score_candidates(
    demo_node: UINode,
    runtime_nodes: list[UINode],
) -> np.ndarray:
    """Return similarity scores (N,) for demo_node vs all runtime_nodes."""
    return np.array([node_similarity(demo_node, rn) for rn in runtime_nodes], dtype=np.float64)


# ──────────────────────────────────────────────────────────────────────────── #
# Ambiguity detection (Appendix A.2)                                          #
# ──────────────────────────────────────────────────────────────────────────── #

def softmax(x: np.ndarray, tau: float = 0.02) -> np.ndarray:
    scaled = x / tau
    scaled -= scaled.max()
    e = np.exp(scaled)
    return e / e.sum()


def normalized_entropy(scores: np.ndarray, tau: float = 0.02) -> float:
    """Normalised entropy of the top-k softmax distribution.

    Uses effective candidate count k_eff = max(|{j: p_j > 0.01}|, 2).
    """
    if len(scores) == 0:
        return 1.0
    p = softmax(scores, tau)
    k_eff = max(int((p > 0.01).sum()), 2)
    entropy = -np.sum(p * np.log(np.maximum(p, 1e-12)))
    return float(entropy / np.log(k_eff))


def is_unambiguous(scores: np.ndarray, min_score: float, max_entropy: float) -> bool:
    """Two-stage gate: top score ≥ min_score AND entropy ≤ max_entropy."""
    if len(scores) == 0:
        return False
    if float(scores.max()) < min_score:
        return False
    return normalized_entropy(scores) <= max_entropy
