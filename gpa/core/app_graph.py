"""Cross-workflow App knowledge graph.

Individual workflows are linear demonstrations. Aggregating many demonstrations
for the same application into a shared navigation graph lets replay reuse
app-level structure and suggest common next actions — the idea behind UI-KOBE
(arXiv:2605.29534) and AppAgentX (arXiv:2503.02268).

Nodes are normalized UI-state/step signatures (per app); edges are observed
transitions between consecutive steps, with counts. This module is a pure,
dependency-light data structure: build it from stored workflows, query it at
replay time, and serialize it for the community layer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

_STATE_PREFIX = "state:"


def _normalize_app(app: str) -> str:
    app = str(app or "").strip()
    if app.casefold() in {"google chrome", "chrome"}:
        return "Google Chrome"
    return app or "unknown"


def _signature(action: str, action_type: str, value: str) -> str:
    """Stable, human-readable signature for a step's UI intent."""
    base = str(action or "").strip() or str(value or "").strip() or str(action_type or "step")
    base = re.sub(r"\s+", " ", base).strip().casefold()
    # Collapse substituted values / long text so equivalent steps merge.
    base = re.sub(r"\{\{[^}]+\}\}", "<var>", base)
    base = base[:80]
    return f"{action_type or 'step'}::{base}"


@dataclass
class AppGraphNode:
    key: str
    app: str
    label: str
    action_type: str
    workflow_ids: set[str] = field(default_factory=set)
    visit_count: int = 0

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "app": self.app,
            "label": self.label,
            "action_type": self.action_type,
            "workflow_ids": sorted(self.workflow_ids),
            "visit_count": self.visit_count,
        }


@dataclass
class AppGraphEdge:
    src: str
    dst: str
    action_type: str
    action: str
    count: int = 0
    workflow_ids: set[str] = field(default_factory=set)

    def to_dict(self) -> dict:
        return {
            "src": self.src,
            "dst": self.dst,
            "action_type": self.action_type,
            "action": self.action,
            "count": self.count,
            "workflow_ids": sorted(self.workflow_ids),
        }


class AppGraph:
    """Directed multigraph of UI-state transitions for a single application."""

    def __init__(self, app: str):
        self.app = _normalize_app(app)
        self.nodes: dict[str, AppGraphNode] = {}
        self.edges: dict[tuple[str, str, str], AppGraphEdge] = {}

    def _touch_node(self, action: str, action_type: str, value: str, workflow_id: str) -> str:
        key = f"{self.app}|{_signature(action, action_type, value)}"
        node = self.nodes.get(key)
        if node is None:
            label = re.sub(r"\s+", " ", str(action or value or action_type or "")).strip() or action_type
            node = AppGraphNode(key=key, app=self.app, label=label[:120], action_type=action_type)
            self.nodes[key] = node
        node.visit_count += 1
        if workflow_id:
            node.workflow_ids.add(workflow_id)
        return key

    def add_transition(
        self,
        src_action: str,
        src_type: str,
        src_value: str,
        dst_action: str,
        dst_type: str,
        dst_value: str,
        workflow_id: str = "",
    ) -> None:
        src_key = self._touch_node(src_action, src_type, src_value, workflow_id)
        dst_key = self._touch_node(dst_action, dst_type, dst_value, workflow_id)
        edge_key = (src_key, dst_key, dst_type)
        edge = self.edges.get(edge_key)
        if edge is None:
            edge = AppGraphEdge(
                src=src_key,
                dst=dst_key,
                action_type=dst_type,
                action=re.sub(r"\s+", " ", str(dst_action or "")).strip()[:120],
            )
            self.edges[edge_key] = edge
        edge.count += 1
        if workflow_id:
            edge.workflow_ids.add(workflow_id)

    def node_key_for(self, action: str, action_type: str, value: str = "") -> str:
        return f"{self.app}|{_signature(action, action_type, value)}"

    def suggest_next(self, node_key: str, limit: int = 5) -> list[AppGraphEdge]:
        """Return likely next transitions from a node, most frequent first."""
        outgoing = [e for e in self.edges.values() if e.src == node_key]
        outgoing.sort(key=lambda e: e.count, reverse=True)
        return outgoing[:limit]

    def to_dict(self) -> dict:
        return {
            "app": self.app,
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges.values()],
        }


def build_app_graphs(workflows: Iterable, app: Optional[str] = None) -> dict[str, AppGraph]:
    """Aggregate an iterable of Workflow objects into per-app graphs.

    Consecutive steps sharing the same active app form a transition edge. Steps
    without an app, or an app filtered out by ``app``, are skipped as sources.
    """
    graphs: dict[str, AppGraph] = {}
    target_app = _normalize_app(app) if app else None
    for wf in workflows:
        wf_id = str(getattr(wf, "workflow_id", "") or getattr(wf, "workflow_name", "") or "")
        steps = list(getattr(wf, "steps", []) or [])
        for i in range(len(steps) - 1):
            a, b = steps[i], steps[i + 1]
            app_a = _normalize_app(getattr(a, "active_app_name", "") or "")
            app_b = _normalize_app(getattr(b, "active_app_name", "") or "")
            if app_a == "unknown" or app_a != app_b:
                continue
            if target_app and app_a != target_app:
                continue
            graph = graphs.get(app_a)
            if graph is None:
                graph = AppGraph(app_a)
                graphs[app_a] = graph
            graph.add_transition(
                getattr(a, "action", ""), getattr(a, "action_type", ""), getattr(a, "value", ""),
                getattr(b, "action", ""), getattr(b, "action_type", ""), getattr(b, "value", ""),
                workflow_id=wf_id,
            )
    return graphs
