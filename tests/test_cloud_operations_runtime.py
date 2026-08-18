import unittest

from gpa.cloud_server.operations import (
    OperationalTelemetry,
    SlidingWindowLimiter,
    client_fingerprint,
)


class CloudOperationsRuntimeTests(unittest.TestCase):
    def test_sliding_window_limiter_returns_bounded_retry(self):
        now = [100.0]
        limiter = SlidingWindowLimiter(clock=lambda: now[0])
        self.assertTrue(limiter.check("client", "api", limit=2).allowed)
        self.assertTrue(limiter.check("client", "api", limit=2).allowed)
        blocked = limiter.check("client", "api", limit=2)
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.retry_after_seconds, 60)
        now[0] = 161.0
        self.assertTrue(limiter.check("client", "api", limit=2).allowed)

    def test_telemetry_is_aggregate_and_client_identity_is_hashed(self):
        telemetry = OperationalTelemetry()
        telemetry.observe(status_code=429, latency_ms=4.5, rate_limited=True)
        metrics = telemetry.prometheus()
        self.assertIn("gpa_cloud_rate_limited_total 1", metrics)
        fingerprint = client_fingerprint("203.0.113.10", salt="secret")
        self.assertNotIn("203.0.113.10", fingerprint)
        self.assertEqual(len(fingerprint), 16)


if __name__ == "__main__":
    unittest.main()
