"""Isolated desktop replay worker.

The local Web server supervises this process over JSON Lines.  Native desktop
libraries, screenshot backends and replay-time model clients are intentionally
loaded here rather than in the long-lived HTTP process.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from gpa.replay.worker_protocol import SCHEMA


def _emit(event: str, **payload: Any) -> None:
    print(json.dumps({"schema": SCHEMA, "event": event, **payload}, ensure_ascii=False), flush=True)


def _serialize_result(result) -> dict[str, Any]:
    steps = []
    for item in result.step_results:
        localization = item.localization
        steps.append({
            "step_number": item.step_number,
            "state": item.state.name.lower(),
            "retries": item.retries,
            "error": item.error,
            "duration_seconds": getattr(item, "duration_seconds", 0.0),
            "agent_decision_ms": getattr(item, "agent_decision_ms", 0.0),
            "agent_decision": getattr(item, "agent_decision", {}),
            "corrections": getattr(item, "corrections", []),
            "observation_metrics": getattr(item, "observation_metrics", []),
            "postcondition_verified": getattr(item, "postcondition_verified", None),
            "postcondition_reason": getattr(item, "postcondition_reason", ""),
            "postcondition_attempts": getattr(item, "postcondition_attempts", 0),
            "evidence_source": getattr(item, "evidence_source", ""),
            "localization": None if localization is None else {
                "x": localization.x,
                "y": localization.y,
                "confidence": localization.confidence,
                "method": localization.method,
            },
        })
    return {
        "success": bool(result.success),
        "error": str(result.error or ""),
        "n_steps": int(result.n_steps),
        "n_failed": int(result.n_failed),
        "llm_metrics": list(result.llm_metrics or []),
        "steps": steps,
    }


def _run(request_path: Path) -> int:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if request.get("schema") != SCHEMA:
        raise ValueError("Unsupported desktop replay worker request schema.")

    control_dir = Path(str(request["control_dir"])).resolve()
    stop_path = control_dir / "stop"
    workflows_dir = Path(str(request["workflows_dir"])).resolve()

    from gpa.execution.executor import Executor
    from gpa.storage.workflow import WorkflowStorage

    workflow, subgraphs = WorkflowStorage(workflows_dir).load(str(request["workflow_id"]))
    _emit("ready", pid=os.getpid(), total_steps=len(workflow.steps))

    def should_stop() -> bool:
        return stop_path.exists()

    def on_step_start(step) -> None:
        _emit(
            "step_start",
            step={
                "number": step.step_number,
                "action": step.action,
                "action_type": step.action_type,
            },
        )

    def on_agent_decision(step, decision: dict) -> None:
        _emit(
            "agent_decision",
            step_number=step.step_number,
            decision={
                "action_type": decision.get("action_type", ""),
                "confidence": decision.get("confidence", 0),
                "reason": decision.get("reason", ""),
                "phase": decision.get("phase", ""),
            },
        )

    executor = Executor(
        workflow,
        subgraphs,
        variables={str(k): str(v) for k, v in dict(request.get("variables") or {}).items()},
        readiness_threshold=float(request.get("threshold", 0.6)),
        max_retries=int(request.get("retries", 2)),
        should_stop=should_stop,
        on_step_start=on_step_start,
        on_agent_decision=on_agent_decision,
        agent_first=bool(request.get("agent_first")),
        verify_final_state=bool(request.get("verify_final_state")),
    )
    _emit("result", result=_serialize_result(executor.run()))
    return 0


def _panic_release() -> int:
    from gpa.execution.actions import emergency_release_inputs

    emergency_release_inputs()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GPA isolated desktop replay worker")
    parser.add_argument("--request", type=Path)
    parser.add_argument("--panic-release", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        if args.panic_release:
            return _panic_release()
        if args.request is None:
            raise ValueError("--request is required")
        return _run(args.request.resolve())
    except BaseException as exc:
        _emit("crash", error=f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
