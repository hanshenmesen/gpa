"""Render a portable video trace from a successful Safe Web source run.

This is deliberately not described as a camera or desktop screen recording.
Every frame is derived from public pages fetched during a deterministic replay
and carries the canonical URL, verified terms, content digest and source run ID.
The companion JSON trace lets another Agent verify the video against the same
public evidence instead of trusting pixels alone.
"""
from __future__ import annotations

import hashlib
import json
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gpa.config import STORAGE_DIR
from gpa.execution.safe_web import fetch_public_page
from gpa.storage import WorkflowStorage

TRACE_SCHEMA = "gpa.safe-web-source-trace/v1"


def _font(size: int, *, bold: bool = False):
    from PIL import ImageFont

    candidates = [
        "/System/Library/Fonts/STHeiti Medium.ttc" if bold else
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrapped(value: str, width: int, max_lines: int) -> list[str]:
    lines = textwrap.wrap(" ".join(str(value or "").split()), width=max(12, width))
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" .") + "…"
    return lines or [""]


def _page_frame(
    *,
    workflow_title: str,
    run_id: str,
    page: dict[str, Any],
    page_index: int,
    page_count: int,
    width: int,
    height: int,
):
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), "#eef3f1")
    draw = ImageDraw.Draw(image)
    ink = "#152525"
    muted = "#61706d"
    teal = "#116f68"
    gold = "#c08a2d"
    panel = "#ffffff"
    draw.rectangle((0, 0, width, 74), fill="#123d46")
    draw.text((34, 20), "LIVE PUBLIC SOURCE EVIDENCE", font=_font(21, bold=True), fill="#f6fbfa")
    draw.text((width - 210, 23), f"SOURCE {page_index}/{page_count}", font=_font(15, bold=True), fill="#e9c477")
    draw.rounded_rectangle((28, 96, width - 28, height - 30), radius=20, fill=panel)
    draw.text((52, 118), workflow_title, font=_font(25, bold=True), fill=ink)
    draw.text((52, 158), "Verified public page · HTTP 200 · no desktop input", font=_font(15), fill=teal)

    y = 202
    draw.text((52, y), "CANONICAL URL", font=_font(12, bold=True), fill=muted)
    y += 24
    for line in _wrapped(page.get("final_url") or page.get("url"), 112, 2):
        draw.text((52, y), line, font=_font(15), fill=ink)
        y += 22

    y += 10
    draw.text((52, y), "POSITIVE EVIDENCE", font=_font(12, bold=True), fill=muted)
    y += 25
    positives = " · ".join(page.get("positive_terms") or []) or "HTTP source fetched"
    for line in _wrapped(positives, 104, 3):
        draw.text((52, y), "PASS  " + line, font=_font(15, bold=True), fill=teal)
        y += 23

    negatives = page.get("negative_terms") or []
    if negatives:
        y += 7
        draw.text((52, y), "NEGATIVE EVIDENCE", font=_font(12, bold=True), fill=muted)
        y += 25
        for line in _wrapped(" · ".join(negatives), 104, 2):
            draw.text((52, y), "ABSENT  " + line, font=_font(15, bold=True), fill=gold)
            y += 23

    y += 14
    draw.text((52, y), "SOURCE EXCERPT", font=_font(12, bold=True), fill=muted)
    y += 24
    for line in _wrapped(page.get("excerpt") or "", 112, 4):
        draw.text((52, y), line, font=_font(14), fill=ink)
        y += 20

    digest = str(page.get("content_sha256") or "")
    footer = f"content sha256 {digest[:16]}…  ·  run {run_id}"
    draw.text((52, height - 64), footer, font=_font(12), fill=muted)
    progress_width = int((width - 104) * page_index / max(1, page_count))
    draw.rounded_rectangle((52, height - 47, width - 52, height - 39), radius=4, fill="#dbe6e2")
    draw.rounded_rectangle((52, height - 47, 52 + progress_width, height - 39), radius=4, fill=teal)
    return image


def build_safe_web_source_evidence(
    workflow_id: str,
    run_id: str,
    destination: str | Path,
    *,
    trace_destination: str | Path | None = None,
    storage: WorkflowStorage | None = None,
    runs_dir: str | Path | None = None,
    width: int = 1280,
    height: int = 720,
    fps: float = 10.0,
    seconds_per_source: float = 1.2,
) -> dict[str, Any]:
    """Re-fetch and render every public source proven by a successful run."""
    import cv2
    import numpy as np

    repository = storage or WorkflowStorage()
    workflow, _ = repository.load(workflow_id)
    run_root = Path(runs_dir) if runs_dir is not None else STORAGE_DIR / "runs"
    run_path = run_root / workflow.workflow_id / f"{run_id}.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    if run.get("workflow_id") != workflow.workflow_id or run.get("run_id") != run_id:
        raise ValueError("Run identity does not match the requested Workflow and run ID.")
    if run.get("success") is not True or run.get("status") != "succeeded":
        raise ValueError("Source evidence can only be rendered from a successful run.")
    if int(run.get("steps_run") or 0) != len(workflow.steps):
        raise ValueError("Successful run did not execute every Workflow step.")

    pages: list[dict[str, Any]] = []
    steps = list(workflow.steps)
    for index, step in enumerate(steps):
        if str(step.action_type or "").casefold() != "open_url":
            continue
        final_url, source_text, status = fetch_public_page(str(step.value), timeout=30)
        if status >= 400:
            raise ValueError(f"Public source returned HTTP {status}: {final_url}")
        next_open = next(
            (candidate for candidate in range(index + 1, len(steps))
             if str(steps[candidate].action_type or "").casefold() == "open_url"),
            len(steps),
        )
        positive_terms = [
            str(candidate.value)
            for candidate in steps[index + 1:next_open]
            if str(candidate.action_type or "").casefold() in {"wait_for_text", "assert_text"}
        ]
        negative_terms = [
            str(candidate.value)
            for candidate in steps[index + 1:next_open]
            if str(candidate.action_type or "").casefold() == "assert_not_text"
        ]
        source_folded = source_text.casefold()
        missing = [term for term in positive_terms if term.casefold() not in source_folded]
        unexpected = [term for term in negative_terms if term.casefold() in source_folded]
        if missing or unexpected:
            raise ValueError(
                f"Public source no longer satisfies the run contract: missing={missing}, unexpected={unexpected}"
            )
        normalized_text = "\n".join(line.strip() for line in source_text.splitlines() if line.strip())
        pages.append({
            "source_index": len(pages) + 1,
            "workflow_step": int(step.step_number),
            "url": str(step.value),
            "final_url": final_url,
            "http_status": int(status),
            "characters": len(normalized_text),
            "content_sha256": hashlib.sha256(normalized_text.encode("utf-8")).hexdigest(),
            "positive_terms": list(dict.fromkeys(positive_terms)),
            "negative_terms": list(dict.fromkeys(negative_terms)),
            "excerpt": normalized_text[:600],
            "verified": True,
        })
    if not pages:
        raise ValueError("Workflow has no public source pages to render.")

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(destination), cv2.VideoWriter_fourcc(*"mp4v"), float(fps), (int(width), int(height))
    )
    if not writer.isOpened():
        raise RuntimeError("OpenCV could not open the MP4 evidence writer.")
    frames_per_source = max(1, round(float(fps) * float(seconds_per_source)))
    try:
        for page_index, page in enumerate(pages, 1):
            frame = _page_frame(
                workflow_title=workflow.workflow_title,
                run_id=run_id,
                page=page,
                page_index=page_index,
                page_count=len(pages),
                width=int(width),
                height=int(height),
            )
            bgr = cv2.cvtColor(np.asarray(frame), cv2.COLOR_RGB2BGR)
            for _ in range(frames_per_source):
                writer.write(bgr)
    finally:
        writer.release()

    generated_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    trace = {
        "schema": TRACE_SCHEMA,
        "generated_at": generated_at,
        "workflow_id": workflow.workflow_id,
        "source_run_id": run_id,
        "execution_mode": str(run.get("execution_mode") or ""),
        "run_success": True,
        "steps_run": int(run.get("steps_run") or 0),
        "page_count": len(pages),
        "capture_kind": "safe-web-source-evidence",
        "video": {
            "path": destination.name,
            "width": int(width),
            "height": int(height),
            "fps": float(fps),
            "frames_per_source": frames_per_source,
        },
        "pages": pages,
    }
    trace_path = Path(trace_destination) if trace_destination else destination.with_suffix(".source-trace.json")
    trace_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"video_path": str(destination), "trace_path": str(trace_path), "trace": trace}


__all__ = ["TRACE_SCHEMA", "build_safe_web_source_evidence"]
