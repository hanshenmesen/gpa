"""Sequential Monte Carlo (SMC) localizer.

Implements the tempered SMC sampler from Section 2.3 and Appendix A.4.

Given a demo subgraph and a runtime UI graph, estimates the posterior
p(θ | Z) where θ = [x, y, sx, sy] (target location + scale factors).

Key paper references:
  • Eq. (1):  p(θ|Z) ∝ p(Z|θ) p(θ)
  • Eq. (2):  per-node likelihood with appearance + geometry
  • Eq. (3):  joint log-likelihood with locality weights
  • Eq. (4):  locality weight Gaussian with adaptive σ_loc (Silverman)
  • Eq. (12): tempered sequence π_β(θ) ∝ p(Z|θ)^β p(θ)
  • Algorithm 1: full SMC procedure
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy.stats import norm

from gpa.config import (
    DIRECT_MATCH_MAX_ENTROPY,
    DIRECT_MATCH_MIN_SCORE,
    SCALE_PRIOR_WEIGHT,
    SCALE_SIGMA,
    SIGMA_LOC_MAX,
    SIGMA_LOC_MIN,
    SMC_ESS_TARGET,
    SMC_MAX_STEPS,
    SMC_N_PARTICLES,
    SMC_TOP_K_CANDIDATES,
    SPATIAL_ALPHA,
    SPATIAL_RBASE,
)
from gpa.core.similarity import (
    is_unambiguous,
    score_candidates,
)
from gpa.core.ui_graph import StepSubgraph, UIGraph, UINode

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────── #
# Result dataclass                                                              #
# ──────────────────────────────────────────────────────────────────────────── #

@dataclass
class LocalizationResult:
    x: float
    y: float
    confidence: float         # p̃(Z|θ) × C_spatial
    likelihood_conf: float    # p̃(Z|θ)
    spatial_conf: float       # C_spatial
    method: str               # "direct" | "smc"
    sx: float = 1.0
    sy: float = 1.0


# ──────────────────────────────────────────────────────────────────────────── #
# Locality weight (Appendix A.1, Silverman's rule)                            #
# ──────────────────────────────────────────────────────────────────────────── #

def compute_sigma_loc(displacements: np.ndarray) -> float:
    """Adaptive σ_loc via Silverman's rule on RMS displacement magnitude.

    displacements: (M, 2) displacement vectors from target to each neighbour.
    """
    n = len(displacements)
    if n < 2:
        return 1000.0   # fallback for degenerate case
    mags = np.linalg.norm(displacements, axis=1)
    sigma_hat = float(np.sqrt(np.mean(mags ** 2)))   # RMS distance
    if sigma_hat < 1e-3:
        return SIGMA_LOC_MIN
    bw = 1.06 * sigma_hat * (n ** (-0.2))
    return float(np.clip(bw, SIGMA_LOC_MIN, SIGMA_LOC_MAX))


def locality_weights(displacements: np.ndarray, sigma_loc: float) -> np.ndarray:
    """w_loc(i) = exp(-||v_i||² / (2 σ_loc²))  →  (M,)"""
    sq_dists = np.sum(displacements ** 2, axis=1)
    return np.exp(-sq_dists / (2 * sigma_loc ** 2))


# ──────────────────────────────────────────────────────────────────────────── #
# Scale prior (Appendix B.3)                                                   #
# ──────────────────────────────────────────────────────────────────────────── #

def log_scale_prior(sx: np.ndarray, sy: np.ndarray,
                    ratio_x: float, ratio_y: float) -> np.ndarray:
    """Mixture of two log-normals per axis; axes independent.

    Component 1 (w=0.5): no resize, μ=0 in log-space
    Component 2 (w=0.5): proportional resize, μ=log(ratio) in log-space
    """
    log_sx = np.log(np.maximum(sx, 1e-6))
    log_sy = np.log(np.maximum(sy, 1e-6))
    s = SCALE_SIGMA
    w = SCALE_PRIOR_WEIGHT

    def _mix_lognormal_log(lz, mu_ratio):
        lp1 = np.log(w + 1e-12) + norm.logpdf(lz, 0, s)
        lp2 = np.log(1 - w + 1e-12) + norm.logpdf(lz, mu_ratio, s)
        # log-sum-exp of the two components
        m = np.maximum(lp1, lp2)
        return m + np.log(np.exp(lp1 - m) + np.exp(lp2 - m))

    log_prior = (
        _mix_lognormal_log(log_sx, np.log(max(ratio_x, 1e-6)))
        + _mix_lognormal_log(log_sy, np.log(max(ratio_y, 1e-6)))
    )
    return log_prior


# ──────────────────────────────────────────────────────────────────────────── #
# Geometric tolerance (Appendix B.3)                                          #
# ──────────────────────────────────────────────────────────────────────────── #

def geometric_sigma(dist: float, elem_size: tuple[float, float]) -> float:
    """σ_i = σ_base + α·d_i + β·min(w_i, h_i)"""
    sigma_base = 20.0
    alpha = 0.05
    beta = 0.1
    return sigma_base + alpha * dist + beta * min(elem_size)


# ──────────────────────────────────────────────────────────────────────────── #
# Per-node likelihood  (Eq. 2)                                                #
# ──────────────────────────────────────────────────────────────────────────── #

class CandidateSet:
    """Pre-computed candidates for one demo node in the runtime graph."""

    def __init__(self, demo_node: UINode, runtime_nodes: list[UINode]):
        self.demo_node = demo_node
        self.runtime_nodes = runtime_nodes
        if runtime_nodes:
            self.scores = score_candidates(demo_node, runtime_nodes)
            self.centers = np.array([n.center for n in runtime_nodes])  # (N, 2)
        else:
            self.scores = np.array([])
            self.centers = np.zeros((0, 2))
        self.pmiss = 0.05   # probability mass for "missing" node

    def log_likelihood(
        self,
        predicted_center: np.ndarray,   # (2,) [x, y]
        sigma: float,
        wapp_weights: Optional[np.ndarray] = None,
    ) -> float:
        """log p(Z_v | θ) for one demo node (Eq. 2)."""
        if len(self.runtime_nodes) == 0:
            return np.log(self.pmiss)

        wapp = self.scores if wapp_weights is None else wapp_weights
        # Gaussian spatial likelihood
        diffs = self.centers - predicted_center[None, :]  # (N, 2)
        sq_dists = np.sum(diffs ** 2, axis=1)             # (N,)
        # Use the same [0, 1] spatial affinity as the vectorised path and
        # confidence calculation. A normalized Gaussian density peaks below
        # pmiss for normal UI-sized sigmas, which would erase every match.
        gauss = np.exp(-sq_dists / (2 * sigma ** 2))
        # Weighted joint score per candidate
        joint = wapp * gauss                               # (N,)
        best = float(joint.max()) if len(joint) else 0.0
        return np.log(max(self.pmiss, best))

    def log_likelihood_batch(
        self,
        predicted_centers: np.ndarray,  # (P, 2) for P particles
        sigma: float,
    ) -> np.ndarray:
        """Vectorised Eq. 2 for P particles. Returns (P,)."""
        if len(self.runtime_nodes) == 0:
            return np.full(len(predicted_centers), np.log(self.pmiss))

        wapp = self.scores  # (N,)
        # predicted_centers: (P, 2),  self.centers: (N, 2)
        # diff: (P, N, 2)
        diff = predicted_centers[:, None, :] - self.centers[None, :, :]
        sq = np.sum(diff ** 2, axis=-1)                    # (P, N)
        gauss = np.exp(-sq / (2 * sigma ** 2))             # (P, N)
        joint = wapp[None, :] * gauss                      # (P, N)
        best = joint.max(axis=1)                           # (P,)
        return np.log(np.maximum(self.pmiss, best))


# ──────────────────────────────────────────────────────────────────────────── #
# Joint log-likelihood (Eq. 3)                                                #
# ──────────────────────────────────────────────────────────────────────────── #

class SMCModel:
    """Pre-builds candidate sets and displacement vectors for one demo step."""

    def __init__(
        self,
        subgraph: StepSubgraph,
        runtime_graph: UIGraph,
        live_size: tuple[int, int],
    ):
        if live_size[0] <= 0 or live_size[1] <= 0:
            raise ValueError(f"live_size must be positive; got {live_size!r}")
        self.subgraph = subgraph
        self.runtime_graph = runtime_graph

        target = subgraph.target_node
        if target is None:
            raise ValueError(
                f"Step subgraph target {subgraph.target_element_id!r} is missing from its UI graph"
            )
        neighbors = subgraph.neighbor_nodes
        demo_target_center = np.array(target.center)

        # Screen scale ratios
        dW, dH = subgraph.ui_graph.image_size or [1, 1]
        lW, lH = live_size
        self.ratio_x = lW / max(dW, 1)
        self.ratio_y = lH / max(dH, 1)

        # Demo nodes = [target] + neighbors
        self.demo_nodes: list[UINode] = []
        if target:
            self.demo_nodes.append(target)
        self.demo_nodes.extend(neighbors)

        # Displacement vectors from target to each context node (in demo coords)
        self.displacements: list[np.ndarray] = []
        for n in self.demo_nodes:
            self.displacements.append(n.center - demo_target_center)

        disps_arr = np.array(self.displacements)  # (M, 2)
        self.sigma_loc = compute_sigma_loc(disps_arr)
        self.wloc = locality_weights(disps_arr, self.sigma_loc)  # (M,)

        # Build candidate sets for each demo node
        rt_nodes = runtime_graph.nodes
        self.candidate_sets: list[CandidateSet] = [
            CandidateSet(dn, rt_nodes) for dn in self.demo_nodes
        ]

        # Per-node geometric σ
        self.geo_sigmas: list[float] = []
        for i, dn in enumerate(self.demo_nodes):
            dist = float(np.linalg.norm(self.displacements[i]))
            self.geo_sigmas.append(geometric_sigma(dist, dn.size))

    # ──────────────────────────────────────────────────────────────────── #

    def log_joint_likelihood_batch(
        self,
        x: np.ndarray,    # (P,)
        y: np.ndarray,    # (P,)
        sx: np.ndarray,   # (P,)
        sy: np.ndarray,   # (P,)
    ) -> np.ndarray:
        """Eq. 3: log p(Z|θ) summed over all demo nodes (locality weighted).

        Returns (P,).
        """
        P = len(x)
        log_lk = np.zeros(P, dtype=np.float64)

        for cs, disp, sigma, wl in zip(
            self.candidate_sets,
            self.displacements,
            self.geo_sigmas,
            self.wloc,
            strict=True,
        ):
            # predicted center of demo node i under hypothesis θ
            # c_hat_i(θ) = [x + sx*r_ix, y + sy*r_iy]
            pred_x = x + sx * disp[0]
            pred_y = y + sy * disp[1]
            pred_centers = np.stack([pred_x, pred_y], axis=1)  # (P, 2)
            node_ll = cs.log_likelihood_batch(pred_centers, sigma)  # (P,)
            log_lk += wl * node_ll

        log_prior = log_scale_prior(sx, sy, self.ratio_x, self.ratio_y)
        return log_lk + log_prior


# ──────────────────────────────────────────────────────────────────────────── #
# Particle initialisation                                                      #
# ──────────────────────────────────────────────────────────────────────────── #

def _initialize_particles(
    model: SMCModel,
    n_particles: int,
    live_size: tuple[int, int],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Init particles from top-K candidate back-projections + Gaussian jitter.

    Returns x, y, sx, sy arrays of shape (N,).
    """
    lW, lH = live_size
    target_cs = model.candidate_sets[0] if model.candidate_sets else None

    proposals: list[tuple[float, float]] = []
    if target_cs is not None and len(target_cs.runtime_nodes) > 0:
        top_k = min(SMC_TOP_K_CANDIDATES, len(target_cs.runtime_nodes))
        top_idx = np.argsort(target_cs.scores)[::-1][:top_k]
        for idx in top_idx:
            c = target_cs.centers[idx]
            proposals.append((float(c[0]), float(c[1])))

    if not proposals:
        proposals = [(lW / 2, lH / 2)]

    # Draw particles proportionally from proposals
    per_prop = max(1, n_particles // len(proposals))
    xs, ys = [], []
    jitter = model.sigma_loc * 0.3

    for px, py in proposals:
        n = per_prop
        xs.append(px + np.random.randn(n) * jitter)
        ys.append(py + np.random.randn(n) * jitter)

    x = np.concatenate(xs)[:n_particles]
    y = np.concatenate(ys)[:n_particles]
    # Pad if needed
    while len(x) < n_particles:
        extra = np.random.uniform(0, lW, n_particles - len(x))
        x = np.concatenate([x, extra])
        y = np.concatenate([y, np.random.uniform(0, lH, len(extra))])

    sx = np.ones(n_particles)
    sy = np.ones(n_particles)
    return x[:n_particles], y[:n_particles], sx[:n_particles], sy[:n_particles]


# ──────────────────────────────────────────────────────────────────────────── #
# Algorithm 1: Tempered SMC sampler                                            #
# ──────────────────────────────────────────────────────────────────────────── #

def _effective_sample_size(weights: np.ndarray) -> float:
    return 1.0 / np.sum(weights ** 2)


def _resample(x, y, sx, sy, weights, n):
    idx = np.random.choice(len(x), size=n, replace=True, p=weights)
    return x[idx].copy(), y[idx].copy(), sx[idx].copy(), sy[idx].copy()


def _mh_step(
    x, y, sx, sy, log_w_target: np.ndarray,
    model: SMCModel, beta: float, step_scale: float,
    live_size: tuple[int, int], n_steps: int = 3,
):
    """Metropolis-Hastings rejuvenation targeting π_β."""
    lW, lH = live_size
    new_x, new_y, new_sx, new_sy = x.copy(), y.copy(), sx.copy(), sy.copy()

    for _ in range(n_steps):
        # Proposal: Gaussian random walk
        prop_x = new_x + np.random.randn(len(x)) * step_scale
        prop_y = new_y + np.random.randn(len(y)) * step_scale
        prop_sx = np.clip(new_sx * np.exp(np.random.randn(len(sx)) * 0.05), 0.3, 3.0)
        prop_sy = np.clip(new_sy * np.exp(np.random.randn(len(sy)) * 0.05), 0.3, 3.0)

        # Clip to screen bounds
        prop_x = np.clip(prop_x, 0, lW)
        prop_y = np.clip(prop_y, 0, lH)

        # Evaluate log target for proposals
        log_prop = beta * model.log_joint_likelihood_batch(prop_x, prop_y, prop_sx, prop_sy)
        log_curr = beta * model.log_joint_likelihood_batch(new_x, new_y, new_sx, new_sy)

        # MH accept/reject
        log_alpha = log_prop - log_curr
        accept = np.log(np.random.uniform(size=len(x))) < log_alpha
        new_x = np.where(accept, prop_x, new_x)
        new_y = np.where(accept, prop_y, new_y)
        new_sx = np.where(accept, prop_sx, new_sx)
        new_sy = np.where(accept, prop_sy, new_sy)

    return new_x, new_y, new_sx, new_sy


def _densest_cluster_mean(
    x: np.ndarray,
    y: np.ndarray,
    weights: np.ndarray,
    *,
    radius: float = 50.0,
) -> tuple[float, float]:
    """Return the weighted center of the highest-mass local particle cluster."""
    if len(x) == 0:
        return 0.0, 0.0
    if not (len(x) == len(y) == len(weights)):
        raise ValueError("particle coordinates and weights must have equal lengths")
    if not np.isfinite(radius) or radius <= 0:
        raise ValueError("cluster radius must be finite and greater than zero")

    normalized = np.asarray(weights, dtype=np.float64)
    if not np.all(np.isfinite(normalized)) or float(normalized.sum()) <= 0:
        normalized = np.ones(len(x), dtype=np.float64) / len(x)
    else:
        normalized = normalized / normalized.sum()

    points = np.column_stack([x, y]).astype(np.float64, copy=False)
    distances_sq = np.sum((points[:, None, :] - points[None, :, :]) ** 2, axis=2)
    neighborhood_mass = (distances_sq <= radius ** 2) @ normalized
    seed = int(np.argmax(neighborhood_mass))
    members = distances_sq[seed] <= radius ** 2
    cluster_weights = normalized[members]
    cluster_weights /= cluster_weights.sum()
    wx = float(np.sum(cluster_weights * x[members]))
    wy = float(np.sum(cluster_weights * y[members]))
    return wx, wy


def run_smc(
    model: SMCModel,
    live_size: tuple[int, int],
    n_particles: int = SMC_N_PARTICLES,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Algorithm 1: tempered SMC.

    Returns: x, y, sx, sy (N,) particle arrays + weights (N,).
    """
    if n_particles < 2:
        raise ValueError("n_particles must be at least 2")
    x, y, sx, sy = _initialize_particles(model, n_particles, live_size)
    weights = np.ones(n_particles) / n_particles

    beta = 0.0
    step = 0
    max_delta_beta = 0.3

    while beta < 1.0 and step < SMC_MAX_STEPS:
        step += 1

        # Choose next β to maintain ESS ≥ ESS_TARGET * N
        target_ess = SMC_ESS_TARGET * n_particles
        ll = model.log_joint_likelihood_batch(x, y, sx, sy)
        if not np.all(np.isfinite(ll)):
            raise FloatingPointError("SMC likelihood produced non-finite values")

        # Binary search for Δβ
        lo, hi = 0.0, min(1.0 - beta, max_delta_beta)
        for _ in range(20):
            mid = (lo + hi) / 2
            log_new_w = np.log(np.maximum(weights, 1e-300)) + mid * ll
            log_new_w -= np.max(log_new_w)
            new_w = np.exp(log_new_w)
            s = new_w.sum()
            if not np.isfinite(s) or s <= 0:
                hi = mid
                continue
            new_w /= s
            ess = _effective_sample_size(new_w)
            if ess >= target_ess:
                lo = mid
            else:
                hi = mid

        delta_beta = lo if lo > 1e-6 else min(0.1, 1.0 - beta)
        beta = min(1.0, beta + delta_beta)

        # Importance reweight
        log_weights = np.log(np.maximum(weights, 1e-300)) + delta_beta * ll
        log_weights -= np.max(log_weights)
        weights = np.exp(log_weights)
        s = weights.sum()
        if not np.isfinite(s) or s <= 0:
            weights = np.ones(n_particles) / n_particles
        else:
            weights /= s

        # Resample if ESS drops below threshold
        ess = _effective_sample_size(weights)
        if ess < target_ess:
            x, y, sx, sy = _resample(x, y, sx, sy, weights, n_particles)
            weights = np.ones(n_particles) / n_particles

        # MH rejuvenation
        spread = float(np.sqrt(np.cov(x, aweights=weights + 1e-12)))
        step_scale = max(5.0, spread * 0.3)
        x, y, sx, sy = _mh_step(x, y, sx, sy, None, model, beta, step_scale, live_size)

    return x, y, sx, sy, weights


# ──────────────────────────────────────────────────────────────────────────── #
# Confidence scoring (Appendix A.3)                                           #
# ──────────────────────────────────────────────────────────────────────────── #

def compute_likelihood_confidence(
    model: SMCModel,
    pred_x: float, pred_y: float, pred_sx: float, pred_sy: float,
) -> float:
    """p̃(Z|θ) — Eq. (7-8): locality-weighted average of per-node confidences."""
    node_confs = []

    for cs, disp, sigma, wl in zip(
        model.candidate_sets,
        model.displacements,
        model.geo_sigmas,
        model.wloc,
        strict=True,
    ):
        if len(cs.runtime_nodes) == 0:
            node_confs.append((0.0, wl))
            continue

        pred_node_center = np.array([
            pred_x + pred_sx * disp[0],
            pred_y + pred_sy * disp[1],
        ])

        wapp = cs.scores                   # (N,)
        diffs = cs.centers - pred_node_center[None, :]
        sq = np.sum(diffs ** 2, axis=1)
        gauss = np.exp(-sq / (2 * sigma ** 2))
        joint = wapp * gauss               # (N,)
        log_pmatch = np.log(max(cs.pmiss, float(joint.max())))

        log_pmiss = np.log(cs.pmiss)
        log_pbest = np.log(max(cs.pmiss, float((wapp * 1.0).max())))  # best appearance only

        if log_pbest <= log_pmiss:
            cv = 0.0
        else:
            cv = float(np.clip(
                (log_pmatch - log_pmiss) / (log_pbest - log_pmiss),
                0.0, 1.0,
            ))
        node_confs.append((cv, wl))

    total_wl = sum(wl for _, wl in node_confs)
    if total_wl < 1e-12:
        return 0.0
    return float(sum(cv * wl for cv, wl in node_confs) / total_wl)


def compute_spatial_confidence(
    x: np.ndarray, y: np.ndarray, weights: np.ndarray, sigma_loc: float
) -> float:
    """C_spatial — Eqs. (9-11): Rayleigh CDF of particle spread."""
    mu_x = float(np.sum(weights * x))
    mu_y = float(np.sum(weights * y))

    dx = x - mu_x
    dy = y - mu_y
    sigma_sq = float(np.sum(weights * (dx ** 2 + dy ** 2))) / 2.0  # isotropic

    r = SPATIAL_RBASE + SPATIAL_ALPHA * sigma_loc
    if sigma_sq < 1e-8:
        return 1.0
    return float(1.0 - np.exp(-r ** 2 / (2 * sigma_sq)))


# ──────────────────────────────────────────────────────────────────────────── #
# Direct match fast path                                                       #
# ──────────────────────────────────────────────────────────────────────────── #

def _try_direct_match(model: SMCModel) -> Optional[LocalizationResult]:
    """Fast path: if target has clear top candidate, skip SMC."""
    if not model.candidate_sets:
        return None
    target_cs = model.candidate_sets[0]
    if len(target_cs.runtime_nodes) == 0:
        return None

    scores = target_cs.scores
    if not is_unambiguous(scores, DIRECT_MATCH_MIN_SCORE, DIRECT_MATCH_MAX_ENTROPY):
        return None

    best_idx = int(np.argmax(scores))
    cx, cy = target_cs.centers[best_idx]
    best_score = float(scores[best_idx])

    return LocalizationResult(
        x=float(cx), y=float(cy),
        confidence=best_score,
        likelihood_conf=best_score,
        spatial_conf=1.0,
        method="direct",
    )


# ──────────────────────────────────────────────────────────────────────────── #
# Public API                                                                   #
# ──────────────────────────────────────────────────────────────────────────── #

def localize(
    subgraph: StepSubgraph,
    runtime_graph: UIGraph,
    live_size: tuple[int, int],
    n_particles: int = SMC_N_PARTICLES,
) -> LocalizationResult:
    """Locate the target element in the runtime screenshot.

    Tries direct match first; falls back to full SMC if ambiguous.
    """
    if n_particles < 2:
        raise ValueError("n_particles must be at least 2")
    model = SMCModel(subgraph, runtime_graph, live_size)

    # Fast path
    direct = _try_direct_match(model)
    if direct is not None:
        logger.debug(f"Direct match: ({direct.x:.1f}, {direct.y:.1f}) conf={direct.confidence:.3f}")
        return direct

    # Full SMC
    logger.debug("Running SMC sampler …")
    x, y, sx, sy, weights = run_smc(model, live_size, n_particles)

    cluster_radius = float(np.clip(model.sigma_loc * 0.75, 20.0, 150.0))
    pred_x, pred_y = _densest_cluster_mean(x, y, weights, radius=cluster_radius)
    pred_sx = float(np.sum(weights * sx))
    pred_sy = float(np.sum(weights * sy))

    lk_conf = compute_likelihood_confidence(model, pred_x, pred_y, pred_sx, pred_sy)
    sp_conf = compute_spatial_confidence(x, y, weights, model.sigma_loc)
    conf = lk_conf * sp_conf

    logger.debug(
        f"SMC: ({pred_x:.1f}, {pred_y:.1f}) "
        f"lk={lk_conf:.3f} sp={sp_conf:.3f} C={conf:.3f}"
    )
    return LocalizationResult(
        x=pred_x, y=pred_y,
        confidence=conf,
        likelihood_conf=lk_conf,
        spatial_conf=sp_conf,
        method="smc",
        sx=pred_sx, sy=pred_sy,
    )
