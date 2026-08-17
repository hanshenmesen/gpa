import tempfile
import unittest
from pathlib import Path

import numpy as np

from gpa.community.media_probe import probe_recording


class MediaProbeTests(unittest.TestCase):
    def test_probe_decodes_samples_from_real_video(self):
        try:
            import cv2
        except ImportError:
            self.skipTest("OpenCV is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "recording.mp4"
            writer = cv2.VideoWriter(
                str(path),
                cv2.VideoWriter_fourcc(*"mp4v"),
                12.0,
                (64, 48),
            )
            if not writer.isOpened():
                self.skipTest("OpenCV MP4 writer is unavailable")
            try:
                for index in range(12):
                    frame = np.zeros((48, 64, 3), dtype=np.uint8)
                    frame[:, :, index % 3] = 30 + index * 10
                    writer.write(frame)
            finally:
                writer.release()

            report = probe_recording(path)

        self.assertEqual(report["schema"], "gpa.recording-media-probe/v1")
        self.assertEqual(report["status"], "verified")
        self.assertTrue(report["verified"])
        self.assertEqual((report["width"], report["height"]), (64, 48))
        self.assertGreaterEqual(report["decoded_sample_count"], 2)

    def test_probe_rejects_container_header_without_decodable_frames(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "recording.mp4"
            path.write_bytes(b"\x00\x00\x00\x18ftypmp42not-a-video")

            report = probe_recording(path)

        self.assertFalse(report["verified"])
        self.assertEqual(report["status"], "invalid")


if __name__ == "__main__":
    unittest.main()
