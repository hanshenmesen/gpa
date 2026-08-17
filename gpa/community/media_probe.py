"""Decode recording samples and report whether an uploaded video is real media.

This module is intentionally executable as a subprocess.  Native video codecs
must never run inside the long-lived Web server process when inspecting an
untrusted community package.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCHEMA = "gpa.recording-media-probe/v1"


def probe_recording(path: str | Path) -> dict[str, Any]:
    recording_path = Path(path)
    try:
        import cv2
    except Exception as exc:
        return {
            "schema": SCHEMA,
            "status": "unavailable",
            "verified": False,
            "error": f"OpenCV video probe unavailable: {type(exc).__name__}",
        }

    capture = cv2.VideoCapture(str(recording_path))
    try:
        if not capture.isOpened():
            return {
                "schema": SCHEMA,
                "status": "invalid",
                "verified": False,
                "error": "The recording could not be opened by the media decoder.",
            }
        frame_count = max(0, int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0))
        fps = max(0.0, float(capture.get(cv2.CAP_PROP_FPS) or 0.0))
        width = max(0, int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0))
        height = max(0, int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0))
        duration = frame_count / fps if frame_count > 0 and fps > 0 else 0.0
        sample_positions = sorted({0, max(0, frame_count // 2), max(0, frame_count - 3)})
        decoded_positions: list[int] = []
        for position in sample_positions:
            capture.set(cv2.CAP_PROP_POS_FRAMES, position)
            ok, frame = capture.read()
            if ok and frame is not None and getattr(frame, "size", 0) > 0:
                decoded_positions.append(position)
        verified = bool(
            frame_count >= 2
            and fps > 0
            and width > 0
            and height > 0
            and duration >= 0.25
            and len(decoded_positions) >= min(2, len(sample_positions))
        )
        return {
            "schema": SCHEMA,
            "status": "verified" if verified else "invalid",
            "verified": verified,
            "frame_count": frame_count,
            "fps": round(fps, 4),
            "duration_seconds": round(duration, 3),
            "width": width,
            "height": height,
            "sample_count": len(sample_positions),
            "decoded_sample_count": len(decoded_positions),
            "decoded_positions": decoded_positions,
            "error": "" if verified else "The recording did not contain enough decodable video samples.",
        }
    finally:
        capture.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recording", required=True, type=Path)
    args = parser.parse_args(argv)
    json.dump(probe_recording(args.recording), sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SCHEMA", "probe_recording"]
