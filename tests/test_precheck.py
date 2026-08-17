import unittest
from threading import Event
from unittest.mock import patch

from gpa.core.precheck import PrecheckPipeline, check_readiness
from gpa.core.smc import LocalizationResult
from gpa.core.ui_graph import UIGraph


class PrecheckTests(unittest.TestCase):
    def test_readiness_threshold_is_validated_before_localization(self):
        for threshold in (-0.1, 1.1, float("nan")):
            with self.subTest(threshold=threshold):
                with (
                    patch("gpa.core.precheck.localize") as localize,
                    self.assertRaisesRegex(ValueError, "threshold"),
                ):
                    check_readiness(None, None, (100, 100), threshold=threshold)
                localize.assert_not_called()

    def test_readiness_uses_localization_confidence(self):
        localization = LocalizationResult(10, 20, 0.75, 0.75, 1.0, "direct")
        with patch("gpa.core.precheck.localize", return_value=localization):
            ready = check_readiness(None, None, (100, 100), threshold=0.7)
            blocked = check_readiness(None, None, (100, 100), threshold=0.8)

        self.assertTrue(ready.ready)
        self.assertFalse(blocked.ready)
        self.assertIs(ready.result, localization)

    def test_pipeline_rejects_invalid_lookahead(self):
        for lookahead in (-1, True, 1.5):
            with self.subTest(lookahead=lookahead):
                with self.assertRaisesRegex(ValueError, "lookahead"):
                    PrecheckPipeline(lookahead=lookahead)

    def test_stopped_pipeline_does_not_accept_more_work(self):
        pipeline = PrecheckPipeline(lookahead=0)
        pipeline.stop()
        pipeline.submit(0, [None, object()], UIGraph(), (100, 100))
        self.assertEqual(pipeline._queue, [])

    def test_cached_result_is_consumed_only_once(self):
        pipeline = PrecheckPipeline(lookahead=0)
        expected = LocalizationResult(10, 20, 0.9, 0.9, 1.0, "precheck")
        pipeline._cache[2] = type("Result", (), {
            "confidence": 0.9,
            "result": expected,
            "ready": True,
            "step_idx": 2,
        })()

        self.assertIsNotNone(pipeline.try_get(2))
        self.assertIsNone(pipeline.try_get(2))

    def test_invalidate_removes_queued_work_and_blocks_inflight_result(self):
        pipeline = PrecheckPipeline(lookahead=1)
        started = Event()
        release = Event()
        localization = LocalizationResult(10, 20, 0.9, 0.9, 1.0, "precheck")

        def delayed_readiness(*args, **kwargs):
            started.set()
            release.wait(timeout=1.0)
            return type("Result", (), {
                "confidence": 0.9,
                "result": localization,
                "ready": True,
                "step_idx": -1,
            })()

        try:
            with patch("gpa.core.precheck.check_readiness", side_effect=delayed_readiness):
                pipeline.submit(0, [None, object()], UIGraph(), (100, 100))
                self.assertTrue(started.wait(timeout=1.0))
                pipeline.invalidate(1)
                release.set()
                pipeline._executor.join(timeout=0.1)
                self.assertIsNone(pipeline.try_get(1))
        finally:
            release.set()
            pipeline.stop()

    def test_resubmit_replaces_older_queued_observation(self):
        pipeline = PrecheckPipeline(lookahead=0)
        pipeline._lookahead = 1
        first_graph = UIGraph()
        second_graph = UIGraph()

        pipeline.submit(0, [None, object()], first_graph, (100, 100))
        pipeline.submit(0, [None, object()], second_graph, (200, 200))

        self.assertEqual(len(pipeline._queue), 1)
        self.assertIs(pipeline._queue[0][3], second_graph)
        self.assertEqual(pipeline._queue[0][4], (200, 200))


if __name__ == "__main__":
    unittest.main()
