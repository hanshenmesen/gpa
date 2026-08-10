"""Workflow Builder: convert a recording into a structured workflow template.

Steps:
  1. For each recorded event, parse the screenshot into a UIGraph and
     identify the target element (closest node to click coords).
  2. Build StepSubgraph (target + KNN neighbours).
3. Call LLM to:
       - Filter accidental or corrective actions out of the workflow
       - Merge low-level events into task-level steps
       - Assign natural-language step descriptions
       - Extract parameterisable variables (e.g. {{recipient_email}})
       - Generate workflow name, title, description
  4. Return Workflow + step_subgraphs dict ready for storage.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from gpa.config import KNN_K
from gpa.core.ui_graph import StepSubgraph, UIGraph, UINode
from gpa.core.ui_parser import parse_screenshot
from gpa.llm import call_json_llm
from gpa.recording.recorder import Recording, RecordedEvent
from gpa.storage.workflow import Workflow, WorkflowStep, WorkflowVariable

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────── #
# LLM prompts                                                                  #
# ──────────────────────────────────────────────────────────────────────────── #

SYSTEM_PROMPT = """You are an expert at analysing GUI workflow recordings and generating
structured automation templates. Given a list of recorded GUI actions, you must:

1. Use the user's task description as the source of truth for the intended outcome.
2. Keep only necessary, intentional actions required to reproduce the task.
3. Discard accidental clicks, wrong navigation, backtracking, duplicate clicks, exploratory moves,
   corrections, and any action that only undoes a previous mistake.
4. Merge low-level events into one task-level step when they represent a single user intent.
   Examples:
   - multiple TYPE chunks, pauses, backspaces/corrections, or retyping in one field -> one TYPE step
     whose value is the final intended text;
   - repeated identical hotkeys caused by uncertainty -> one HOTKEY step;
   - tiny click jitter on the same target -> one CLICK step using the final intentional click.
   Do not merge actions that must be replayed separately, such as clicking a field and then typing
   into it; those should remain two ordered workflow steps.
5. Assign a concise natural-language description to each retained or merged step.
6. Identify any parameterisable fields — values that a user might want to change each run
   (e.g. email addresses, search terms, dates, form values). Replace those literal values
   with {{variable_name}} placeholders.
7. Generate a short snake_case workflow name, a title-case workflow title, and a one-sentence
   description of what the workflow does.

Return a JSON object with this exact schema:
{
  "task_description": "User's intended task in natural language",
  "workflow_name": "draft_email",
  "workflow_title": "Draft Email",
  "description": "Compose and save a draft email with recipient, subject, and body.",
  "variables": [
    {"name": "recipient_email", "default_value": "user@example.com", "description": "Email address of the recipient"}
  ],
  "steps": [
    {
      "event_indices": [1],
      "action_type": "click",
      "description": "Click on Mail icon",
      "value": "",
      "variables": []
    },
    {
      "event_indices": [3, 4, 5],
      "action_type": "type",
      "description": "Type recipient email",
      "value": "{{recipient_email}}",
      "variables": ["recipient_email"]
    }
  ],
  "discarded_events": [
    {"event_index": 2, "reason": "Accidental click that was immediately corrected"}
  ]
}
event_indices are original 1-based numbers from the recording. Use event_index only for backward
compatibility when a step maps to one event. Preserve the order of retained steps. If an event is
necessary despite looking like navigation or correction, keep it. Keep DRAG events when they select
text or define a target range. Keep copy HOTKEY steps when they capture clipboard text from the
user's selection. For merged TYPE steps, value must be the final intended text after corrections,
not the raw keystroke history.
Only return valid JSON, nothing else."""


def _build_action_summary(events: list[RecordedEvent]) -> str:
    lines = []
    for i, ev in enumerate(events, 1):
        if ev.event_type == "click":
            lines.append(f"{i}. CLICK at ({ev.x:.0f}, {ev.y:.0f}) in app '{ev.active_app}'")
        elif ev.event_type == "drag":
            lines.append(
                f"{i}. DRAG from ({ev.start_x:.0f}, {ev.start_y:.0f}) "
                f"to ({ev.end_x:.0f}, {ev.end_y:.0f}) in app '{ev.active_app}'"
            )
        elif ev.event_type == "type":
            lines.append(f'{i}. TYPE "{ev.value}"')
        elif ev.event_type == "hotkey":
            copy_suffix = ""
            if ev.clipboard_after:
                preview = ev.clipboard_after.strip().replace("\n", " ")[:160]
                copy_suffix = f' copied {len(ev.clipboard_after)} chars: "{preview}"'
            lines.append(f'{i}. HOTKEY {ev.value}{copy_suffix}')
        elif ev.event_type == "scroll":
            lines.append(f'{i}. SCROLL ({ev.scroll_dx}, {ev.scroll_dy}) at ({ev.x:.0f}, {ev.y:.0f})')
    return "\n".join(lines)


def _call_llm(
    action_summary: str,
    task_description: str = "",
    events: Optional[list[RecordedEvent]] = None,
) -> dict:
    """Call LLM to enrich the step list and extract workflow metadata."""
    user_msg = (
        "User task description:\n"
        f"{task_description or '(not provided)'}\n\n"
        "Recorded GUI actions:\n\n"
        f"{action_summary}\n\n"
        "Generate the filtered workflow template JSON. Only keep actions that are required "
        "to reproduce the user's task."
    )

    for attempt in range(3):
        try:
            return call_json_llm(SYSTEM_PROMPT, user_msg, temperature=0.2, attempts=1)
        except Exception as e:
            logger.warning(f"LLM call attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                time.sleep(2)
    # Fallback: generate minimal template without LLM
    logger.error("All LLM attempts failed; generating fallback template.")
    return _fallback_template(events or [], action_summary, task_description)


def _fallback_template(
    events: list[RecordedEvent],
    action_summary: str = "",
    task_description: str = "",
) -> dict:
    if events:
        return {
            "task_description": task_description,
            "workflow_name": "workflow_" + str(uuid.uuid4())[:8],
            "workflow_title": "Recorded Workflow",
            "description": "Automatically recorded GUI workflow.",
            "variables": [],
            "steps": _local_merge_steps(events),
            "discarded_events": [],
        }

    lines = action_summary.strip().split("\n")
    steps = []
    for i, line in enumerate(lines, 1):
        parts = line.split(". ", 1)
        desc = parts[1] if len(parts) > 1 else line
        steps.append({"event_indices": [i], "description": desc, "variables": []})
    return {
        "task_description": task_description,
        "workflow_name": "workflow_" + str(uuid.uuid4())[:8],
        "workflow_title": "Recorded Workflow",
        "description": "Automatically recorded GUI workflow.",
        "variables": [],
        "steps": steps,
        "discarded_events": [],
    }


def _local_merge_steps(events: list[RecordedEvent]) -> list[dict]:
    """Conservative fallback merger for common low-level recording noise."""
    steps: list[dict] = []
    i = 0
    while i < len(events):
        event = events[i]
        event_index = i + 1

        if event.event_type == "type":
            indices = [event_index]
            text_parts = [event.value]
            j = i + 1
            while j < len(events) and events[j].event_type == "type" and events[j].active_app == event.active_app:
                indices.append(j + 1)
                text_parts.append(events[j].value)
                j += 1
            value = "".join(text_parts)
            steps.append({
                "event_indices": indices,
                "action_type": "type",
                "description": f"Type text: {value}",
                "value": value,
                "variables": [],
            })
            i = j
            continue

        if event.event_type == "hotkey":
            indices = [event_index]
            j = i + 1
            while (
                j < len(events)
                and events[j].event_type == "hotkey"
                and events[j].value == event.value
                and events[j].active_app == event.active_app
                and events[j].pause_before <= 1.0
            ):
                indices.append(j + 1)
                j += 1
            steps.append({
                "event_indices": indices,
                "action_type": "hotkey",
                "description": f"Press {event.value}",
                "value": event.value,
                "variables": [],
            })
            i = j
            continue

        if event.event_type == "drag":
            steps.append({
                "event_indices": [event_index],
                "action_type": "drag",
                "description": _event_description(event, event_index),
                "value": "",
                "variables": [],
            })
            i += 1
            continue

        steps.append({
            "event_indices": [event_index],
            "action_type": event.event_type,
            "description": _event_description(event, event_index),
            "value": event.value,
            "variables": [],
        })
        i += 1
    return steps


def _event_description(event: RecordedEvent, event_index: int) -> str:
    if event.event_type == "click":
        return f"Click at ({event.x:.0f}, {event.y:.0f})"
    if event.event_type == "drag":
        return (
            f"Drag from ({event.start_x:.0f}, {event.start_y:.0f}) "
            f"to ({event.end_x:.0f}, {event.end_y:.0f})"
        )
    if event.event_type == "scroll":
        return f"Scroll at ({event.x:.0f}, {event.y:.0f})"
    if event.event_type == "type":
        return f"Type text: {event.value}"
    if event.event_type == "hotkey":
        return f"Press {event.value}"
    return f"Step {event_index}: {event.event_type}"


# ──────────────────────────────────────────────────────────────────────────── #
# Build subgraphs from recording events                                        #
# ──────────────────────────────────────────────────────────────────────────── #

def _build_subgraph(event: RecordedEvent) -> Optional[StepSubgraph]:
    """Parse the screenshot, find target node, build subgraph."""
    target_x = event.start_x if event.event_type == "drag" and event.start_x else event.x
    target_y = event.start_y if event.event_type == "drag" and event.start_y else event.y
    if event.screenshot is None:
        return _coordinate_subgraph(event)
    try:
        graph = parse_screenshot(event.screenshot, knn_k=KNN_K)
    except Exception as e:
        logger.warning(f"UI parse failed for event at ({target_x}, {target_y}): {e}")
        return _coordinate_subgraph(event)

    if not graph.nodes:
        logger.warning("No UI nodes detected; using recorded coordinates.")
        return _coordinate_subgraph(event)

    # Find target element at click coordinates
    target = graph.node_at(target_x, target_y)
    if target is None:
        target = graph.closest_node(target_x, target_y)

    if target is None:
        return None

    window_bounds = graph.window_bounds or [0, 0, event.screenshot.width, event.screenshot.height]

    return StepSubgraph(
        target_element_id=target.id,
        click_coordinates=[target_x, target_y],
        ui_graph=graph,
        window_bounds=window_bounds,
        knn_k=KNN_K,
        scale_factor=1.0,
    )


def _coordinate_subgraph(event: RecordedEvent) -> StepSubgraph:
    """Fallback context that preserves replay coordinates when vision is unavailable."""
    x = event.start_x if event.event_type == "drag" and event.start_x else event.x
    y = event.start_y if event.event_type == "drag" and event.start_y else event.y
    image_width = event.screenshot.width if event.screenshot is not None else max(1, int(x) * 2)
    image_height = event.screenshot.height if event.screenshot is not None else max(1, int(y) * 2)
    box_size = 16.0
    node = UINode(
        id=0,
        pos=[
            max(0.0, float(x) - box_size / 2),
            max(0.0, float(y) - box_size / 2),
            box_size,
            box_size,
        ],
        elem_type="icon",
        content="recorded coordinate",
    )
    graph = UIGraph(nodes=[node], image_size=[image_width, image_height])
    return StepSubgraph(
        target_element_id=0,
        click_coordinates=[x, y],
        ui_graph=graph,
        window_bounds=[0, 0, image_width, image_height],
        knn_k=KNN_K,
        scale_factor=1.0,
    )


# ──────────────────────────────────────────────────────────────────────────── #
# Public API                                                                   #
# ──────────────────────────────────────────────────────────────────────────── #

@dataclass
class BuildResult:
    workflow: Workflow
    step_subgraphs: dict[str, StepSubgraph]   # step_id → subgraph


def _event_index(item: dict, fallback: int) -> int:
    raw = item.get("event_index", item.get("step_number", fallback))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return fallback


def _event_indices(item: dict, fallback: int, event_count: int) -> list[int]:
    raw = item.get("event_indices")
    if raw is None:
        raw = [item.get("event_index", item.get("step_number", fallback))]
    if not isinstance(raw, list):
        raw = [raw]

    indices = []
    for value in raw:
        try:
            index = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= index <= event_count and index not in indices:
            indices.append(index)
    return indices


def _replace_variable_defaults(value: str, variables: list[WorkflowVariable]) -> str:
    result = value
    for var in variables:
        default = str(var.default_value)
        if default:
            result = result.replace(default, f"{{{{{var.name}}}}}")
    return result


def _merged_step_action(
    events: list[RecordedEvent],
    llm_step: dict,
    variables: list[WorkflowVariable],
) -> tuple[RecordedEvent, str, str, float, str]:
    """Return representative event plus action_type, value, pause, active app."""
    action_type = str(llm_step.get("action_type") or "").strip().lower()
    if action_type not in {"click", "scroll", "type", "hotkey", "drag"}:
        event_types = [event.event_type for event in events]
        action_type = max(set(event_types), key=event_types.count)

    same_type_events = [event for event in events if event.event_type == action_type]
    representative = same_type_events[-1] if same_type_events else events[-1]

    if "value" in llm_step:
        value = str(llm_step.get("value") or "")
    elif action_type == "type":
        value = "".join(event.value for event in events if event.event_type == "type")
    elif action_type == "hotkey":
        values = [event.value for event in events if event.event_type == "hotkey"]
        value = values[-1] if values else representative.value
    else:
        value = representative.value

    if action_type == "type":
        value = _replace_variable_defaults(value, variables)

    pause = events[0].pause_before
    active_app = representative.active_app
    return representative, action_type, value, pause, active_app


def _merged_step_metadata(events: list[RecordedEvent], action_type: str) -> dict:
    metadata: dict = {}
    scroll_events = [event for event in events if event.event_type == "scroll"]
    if action_type == "scroll" and scroll_events:
        metadata.update({
            "scroll_dx": sum(int(event.scroll_dx) for event in scroll_events),
            "scroll_dy": sum(int(event.scroll_dy) for event in scroll_events),
        })

    drag_events = [event for event in events if event.event_type == "drag"]
    if drag_events:
        event = drag_events[-1]
        key_prefix = "drag" if action_type == "drag" else "selection_drag"
        metadata.update({
            f"{key_prefix}_start": [event.start_x, event.start_y],
            f"{key_prefix}_end": [event.end_x, event.end_y],
            f"{key_prefix}_duration_seconds": event.duration_seconds,
            f"{key_prefix}_button": event.button or "left",
        })

    copy_events = [
        event for event in events
        if event.event_type == "hotkey"
        and str(event.value or "").strip().casefold().replace("command", "cmd")
        in {"cmd+c", "cmd+copy", "ctrl+c"}
        and event.clipboard_after
    ]
    if copy_events:
        event = copy_events[-1]
        metadata.update({
            "recorded_clipboard_text": event.clipboard_after,
            "recorded_clipboard_length": len(event.clipboard_after or ""),
            "recorded_clipboard_changed": event.clipboard_after.strip() != event.clipboard_before.strip(),
        })
        if event.clipboard_before:
            metadata["recorded_clipboard_before"] = event.clipboard_before
    return metadata


def _is_console_browser_focus_noise(step: WorkflowStep, workflow_text: str) -> bool:
    if str(step.active_app_name or "").strip().casefold() != "codex":
        return False
    if step.action_type not in {"click", "scroll"}:
        return False
    text = " ".join([workflow_text, step.action or ""]).casefold()
    compact = "".join(ch for ch in text if ch.isalnum())
    browser_goal = (
        any(token in text for token in ("browser", "chrome", "safari", "web", "网页", "浏览器"))
        or "acmtechnews" in compact
    )
    focus_noise = any(
        token in text
        for token in (
            "content",
            "focus",
            "loaded",
            "page",
            "browser",
            "网页",
            "页面",
            "内容",
            "聚焦",
        )
    )
    return browser_goal and focus_noise


def _prune_console_noise_steps(
    workflow_steps: list[WorkflowStep],
    step_subgraphs: dict[str, StepSubgraph],
    workflow_text: str,
) -> list[WorkflowStep]:
    pruned: list[WorkflowStep] = []
    removed_ids = set()
    for step in workflow_steps:
        if _is_console_browser_focus_noise(step, workflow_text):
            logger.warning(
                "Dropping likely console noise step %s from workflow build: %s",
                step.step_number,
                step.action,
            )
            removed_ids.add(step.id)
            continue
        pruned.append(step)

    for step_id in removed_ids:
        step_subgraphs.pop(step_id, None)
    for index, step in enumerate(pruned, 1):
        step.step_number = index
    return pruned


def build_workflow(
    recording: Recording,
    workflow_id: Optional[str] = None,
    task_description: str = "",
) -> BuildResult:
    """Convert a Recording into a Workflow + per-step subgraphs.

    Args:
        recording: captured events from Recorder.stop()
        workflow_id: optional override; auto-generated if None
        task_description: user's natural-language task goal

    Returns:
        BuildResult with Workflow and step_subgraphs dict
    """
    if not recording.events:
        raise ValueError("Recording is empty — nothing to build.")

    wid = workflow_id or time.strftime("%Y%m%d_%H%M%S") + "_" + str(uuid.uuid4())[:4]

    # 1. Call LLM to enrich steps
    logger.info("Analysing workflow with LLM …")
    summary = _build_action_summary(recording.events)
    llm_result = _call_llm(summary, task_description, recording.events)

    # 2. Map LLM-retained or merged steps back to original events by event_indices.
    llm_steps = llm_result.get("steps", [])
    if not isinstance(llm_steps, list):
        llm_steps = []
    retained_steps = []
    seen_event_indices = set()
    for fallback_index, item in enumerate(llm_steps, 1):
        if not isinstance(item, dict):
            continue
        indices = _event_indices(item, fallback_index, len(recording.events))
        indices = [index for index in indices if index not in seen_event_indices]
        if not indices:
            continue
        seen_event_indices.update(indices)
        retained_steps.append((indices, item))
    if not retained_steps:
        logger.warning("LLM returned no valid retained steps; keeping all recorded events.")
        retained_steps = [
            ([i], {"event_indices": [i], "description": f"Step {i}: {event.event_type}"})
            for i, event in enumerate(recording.events, 1)
        ]

    variables = [
        WorkflowVariable(
            name=str(v.get("name", "")).strip(),
            default_value=v.get("default_value", ""),
            description=v.get("description", ""),
        )
        for v in llm_result.get("variables", [])
        if isinstance(v, dict) and str(v.get("name", "")).strip()
    ]

    # 3. Build UIGraph + subgraph only for retained events.
    logger.info("Parsing screenshots and building UI graphs …")
    workflow_steps: list[WorkflowStep] = []
    step_subgraphs: dict[str, StepSubgraph] = {}

    for step_number, (event_indices, llm_step) in enumerate(retained_steps, 1):
        events = [recording.events[index - 1] for index in event_indices]
        representative_event, action_type, value, pause, active_app = _merged_step_action(
            events,
            llm_step,
            variables,
        )
        step_metadata = _merged_step_metadata(events, action_type)
        step_id = str(uuid.uuid4())
        description = llm_step.get("description", f"Step {step_number}: {action_type}")
        # Replace variable placeholders with markers
        for var in variables:
            description = description.replace(f"{{{{{var.name}}}}}", f"{{{{{var.name}}}}}")

        ws = WorkflowStep(
            step_number=step_number,
            action=description,
            id=step_id,
            action_type=action_type,
            value=value,
            pause_duration=pause,
            active_app_name=active_app,
            metadata=step_metadata,
        )
        workflow_steps.append(ws)

        # Coordinate-based events need replay context.
        if action_type in ("click", "scroll", "drag"):
            sg = _build_subgraph(representative_event)
            if sg is not None:
                step_subgraphs[step_id] = sg
            else:
                logger.warning(f"Step {step_number}: could not build subgraph.")

    discarded = llm_result.get("discarded_events", [])
    if isinstance(discarded, list) and discarded:
        logger.info("LLM discarded %s recording event(s) as non-essential.", len(discarded))

    workflow_text = " ".join([
        str(llm_result.get("task_description") or task_description or ""),
        str(llm_result.get("description") or ""),
        str(llm_result.get("workflow_title") or ""),
    ])
    workflow_steps = _prune_console_noise_steps(workflow_steps, step_subgraphs, workflow_text)

    workflow = Workflow(
        workflow_id=wid,
        workflow_name=llm_result.get("workflow_name", f"workflow_{wid[:8]}"),
        workflow_title=llm_result.get("workflow_title", "Recorded Workflow"),
        description=llm_result.get("description", ""),
        variables=variables,
        steps=workflow_steps,
        task_description=llm_result.get("task_description") or task_description,
    )

    logger.info(
        f"Workflow '{workflow.workflow_name}' built: "
        f"{len(workflow_steps)} steps, {len(variables)} variables, "
        f"{len(step_subgraphs)} subgraphs."
    )
    return BuildResult(workflow=workflow, step_subgraphs=step_subgraphs)
