"""UI Graph data structure.

Nodes store bounding boxes, OCR text, icon embeddings (IconCLIP 512-d),
and text embeddings (Sentence-E5 384-d). Edges connect spatially nearby
elements via KNN. Mirrors the steps_data.json format from the paper.
"""
from __future__ import annotations

import json
import inspect
from dataclasses import dataclass, field
from typing import Optional
import numpy as np
import networkx as nx


def _node_link_data(graph: nx.Graph) -> dict:
    """Use a stable edge field across NetworkX versions."""
    if "edges" in inspect.signature(nx.node_link_data).parameters:
        return nx.node_link_data(graph, edges="edges")
    return nx.node_link_data(graph, link="edges")


def _node_link_graph(data: dict) -> nx.Graph:
    edge_field = "edges" if "edges" in data else "links"
    if "edges" in inspect.signature(nx.node_link_graph).parameters:
        return nx.node_link_graph(data, edges=edge_field)
    return nx.node_link_graph(data, link=edge_field)


@dataclass
class UINode:
    id: int
    # bounding box [x, y, w, h] in screen pixels
    pos: list[float]
    # element type: "icon" | "text"
    elem_type: str
    # OCR text (None for pure icons)
    content: Optional[str]
    # IconCLIP ViT-B-32 embedding (512-d)
    icon_emb: Optional[np.ndarray] = field(default=None, repr=False)
    # Sentence-E5 embedding (384-d), only for text nodes
    text_emb: Optional[np.ndarray] = field(default=None, repr=False)

    @property
    def center(self) -> np.ndarray:
        x, y, w, h = self.pos
        return np.array([x + w / 2, y + h / 2], dtype=np.float64)

    @property
    def size(self) -> tuple[float, float]:
        return self.pos[2], self.pos[3]

    def to_dict(self) -> dict:
        d: dict = {
            "id": self.id,
            "pos": self.pos,
            "attrs": {"type": self.elem_type, "content": self.content},
        }
        if self.icon_emb is not None:
            d["icon_emb"] = self.icon_emb.tolist()
        if self.text_emb is not None:
            d["text_emb"] = self.text_emb.tolist()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "UINode":
        attrs = d.get("attrs", {})
        icon_emb = np.array(d["icon_emb"]) if "icon_emb" in d else None
        text_emb = np.array(d["text_emb"]) if "text_emb" in d else None
        return cls(
            id=d["id"],
            pos=d["pos"],
            elem_type=attrs.get("type", "icon"),
            content=attrs.get("content"),
            icon_emb=icon_emb,
            text_emb=text_emb,
        )


@dataclass
class UIGraph:
    """Undirected KNN graph over UI elements for one screenshot."""
    nodes: list[UINode] = field(default_factory=list)
    # edges as (id_a, id_b)
    edges: list[tuple[int, int]] = field(default_factory=list)
    # original image size [W, H]
    image_size: list[int] = field(default_factory=lambda: [0, 0])
    # window bounds [x, y, w, h] in screen coordinates
    window_bounds: Optional[list[float]] = None
    # Runtime-only parser metrics; intentionally omitted from serialized workflow data.
    parse_metrics: dict = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ #
    # Build                                                                #
    # ------------------------------------------------------------------ #

    def build_knn_edges(self, k: int = 8) -> None:
        """Connect each node to its k nearest neighbours by centre distance."""
        if len(self.nodes) < 2:
            return
        centers = np.array([n.center for n in self.nodes])  # (N, 2)
        self.edges = []
        for i, ci in enumerate(centers):
            dists = np.linalg.norm(centers - ci, axis=1)
            dists[i] = np.inf
            nn_ids = np.argsort(dists)[:k]
            for j in nn_ids:
                edge = (min(i, int(j)), max(i, int(j)))
                if edge not in self.edges:
                    self.edges.append(edge)

    # ------------------------------------------------------------------ #
    # Query                                                                #
    # ------------------------------------------------------------------ #

    def get_node(self, node_id: int) -> Optional[UINode]:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def neighbors_of(self, node_id: int) -> list[UINode]:
        nbr_ids = set()
        for a, b in self.edges:
            if a == node_id:
                nbr_ids.add(b)
            elif b == node_id:
                nbr_ids.add(a)
        return [n for n in self.nodes if n.id in nbr_ids]

    def node_at(self, x: float, y: float) -> Optional[UINode]:
        """Return node whose bounding box contains (x, y)."""
        for n in self.nodes:
            nx_, ny, nw, nh = n.pos
            if nx_ <= x <= nx_ + nw and ny <= y <= ny + nh:
                return n
        return None

    def closest_node(self, x: float, y: float) -> Optional[UINode]:
        if not self.nodes:
            return None
        pt = np.array([x, y])
        centers = np.array([n.center for n in self.nodes])
        idx = int(np.argmin(np.linalg.norm(centers - pt, axis=1)))
        return self.nodes[idx]

    # ------------------------------------------------------------------ #
    # Serialization (networkx node-link format used in steps_data.json)  #
    # ------------------------------------------------------------------ #

    def to_nx_dict(self) -> dict:
        G = nx.Graph()
        for n in self.nodes:
            G.add_node(n.id, **n.to_dict())
        for a, b in self.edges:
            G.add_edge(a, b)
        return _node_link_data(G)

    @classmethod
    def from_nx_dict(cls, d: dict, image_size=None, window_bounds=None) -> "UIGraph":
        G = _node_link_graph(d)
        # networkx node_link_graph does NOT store the node id in node attributes,
        # so we pass the node id explicitly when reconstructing UINode objects.
        nodes = [UINode.from_dict({"id": n, **G.nodes[n]}) for n in sorted(G.nodes())]
        edges = [(int(u), int(v)) for u, v in G.edges()]
        return cls(
            nodes=nodes,
            edges=edges,
            image_size=image_size or [0, 0],
            window_bounds=window_bounds,
        )

    def to_dict(self) -> dict:
        return {
            "G": self.to_nx_dict(),
            "image_size": self.image_size,
            "window_bounds": self.window_bounds,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "UIGraph":
        return cls.from_nx_dict(
            d["G"],
            image_size=d.get("image_size", [0, 0]),
            window_bounds=d.get("window_bounds"),
        )


@dataclass
class StepSubgraph:
    """Per-step demo subgraph: target node + KNN neighbourhood."""
    target_element_id: int
    click_coordinates: list[float]       # [x, y] in screen pixels
    ui_graph: UIGraph
    window_bounds: list[float]           # [x, y, w, h]
    knn_k: int = 8
    scale_factor: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0

    @property
    def target_node(self) -> Optional[UINode]:
        return self.ui_graph.get_node(self.target_element_id)

    @property
    def neighbor_nodes(self) -> list[UINode]:
        return self.ui_graph.neighbors_of(self.target_element_id)

    def to_dict(self) -> dict:
        return {
            "target_element_id": self.target_element_id,
            "image_size": self.ui_graph.image_size,
            "click_coordinates": self.click_coordinates,
            "ui_graph": self.ui_graph.to_dict(),
            "window_bounds": self.window_bounds,
            "knn_k": self.knn_k,
            "scale_factor": self.scale_factor,
            "offset_x": self.offset_x,
            "offset_y": self.offset_y,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StepSubgraph":
        g = UIGraph.from_dict(d["ui_graph"])
        return cls(
            target_element_id=d["target_element_id"],
            click_coordinates=d["click_coordinates"],
            ui_graph=g,
            window_bounds=d["window_bounds"],
            knn_k=d.get("knn_k", 8),
            scale_factor=d.get("scale_factor", 1.0),
            offset_x=d.get("offset_x", 0.0),
            offset_y=d.get("offset_y", 0.0),
        )
