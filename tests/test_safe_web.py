import socket
import unittest
from http.client import IncompleteRead
from unittest.mock import patch
from urllib.error import HTTPError

from gpa.execution.safe_web import (
    SAFE_WEB_USER_AGENT,
    SafeWebRunner,
    public_http_url_error,
    safe_web_compatibility,
    static_public_url_error,
)
from gpa.storage.workflow import Workflow, WorkflowStep


def safe_workflow(*steps: WorkflowStep) -> Workflow:
    return Workflow(
        workflow_id="safe-web-fixture",
        workflow_name="safe-web-fixture",
        workflow_title="Safe Web Fixture",
        description="Safe Web test fixture",
        steps=list(steps),
    )


class SafeWebReplayTests(unittest.TestCase):
    def test_safe_web_identifies_itself_without_browser_impersonation(self):
        self.assertTrue(SAFE_WEB_USER_AGENT.startswith("GPA-SafeWeb/"))
        self.assertNotIn("Mozilla", SAFE_WEB_USER_AGENT)

    def test_compatibility_accepts_only_read_only_public_web_steps(self):
        compatible = safe_workflow(
            WorkflowStep(1, "Open source", action_type="open_url", value="https://example.com"),
            WorkflowStep(2, "Check source", action_type="assert_text", value="Example"),
            WorkflowStep(3, "Check absence", action_type="assert_not_text", value="Forbidden"),
            WorkflowStep(4, "Remember answer", action_type="set_clipboard", value="answer"),
            WorkflowStep(5, "Check answer", action_type="assert_clipboard", value="answer"),
        )
        unsafe = safe_workflow(
            WorkflowStep(1, "Open source", action_type="open_url", value="https://example.com"),
            WorkflowStep(2, "Type globally", action_type="type", value="answer"),
        )

        report = safe_web_compatibility(compatible)
        self.assertTrue(report["runnable"])
        self.assertFalse(report["uses_desktop_input"])
        self.assertFalse(report["uses_system_clipboard"])
        self.assertFalse(report["uses_llm"])
        self.assertFalse(safe_web_compatibility(unsafe)["runnable"])
        self.assertIn("assert_link", report["supported_actions"])
        self.assertIn("assert_not_text", report["supported_actions"])

    def test_runner_proves_expected_text_absence(self):
        workflow = safe_workflow(
            WorkflowStep(1, "Open source", action_type="open_url", value="https://example.test/report"),
            WorkflowStep(2, "Prove no snow", action_type="assert_not_text", value="Snow"),
        )
        with patch(
            "gpa.execution.safe_web.fetch_public_page",
            return_value=("https://example.test/report", "Chicago precipitation: 0.00", 200),
        ):
            result = SafeWebRunner(workflow).run()
        self.assertTrue(result.success)
        self.assertTrue(result.step_results[1].postcondition_verified)

        with patch(
            "gpa.execution.safe_web.fetch_public_page",
            return_value=("https://example.test/report", "Chicago Snow", 200),
        ):
            failed = SafeWebRunner(workflow).run()
        self.assertFalse(failed.success)
        self.assertIn("Unexpected public source text", failed.error)

    def test_public_url_policy_blocks_local_private_and_credentialed_targets(self):
        for target in (
            "http://localhost:8765/store",
            "http://127.0.0.1/private",
            "http://10.0.0.2/private",
            "http://user:password@example.com/private",
            "file:///etc/passwd",
        ):
            with self.subTest(target=target):
                self.assertTrue(static_public_url_error(target))

    def test_dns_resolution_rejects_private_addresses(self):
        private_result = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.4", 443))]
        public_result = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("gpa.execution.safe_web.socket.getaddrinfo", return_value=private_result):
            self.assertIn("non-public", public_http_url_error("https://example.com"))
        with patch("gpa.execution.safe_web.socket.getaddrinfo", return_value=public_result):
            self.assertEqual(public_http_url_error("https://example.com"), "")

    def test_runner_uses_public_evidence_and_run_local_memory_only(self):
        workflow = safe_workflow(
            WorkflowStep(1, "Open source", action_type="open_url", value="https://example.test/report"),
            WorkflowStep(2, "Check URL", action_type="assert_url", value="example.test/report"),
            WorkflowStep(3, "Check fact", action_type="assert_text", value="Verified fact"),
            WorkflowStep(4, "Store result", action_type="set_clipboard", value="{{answer}}"),
            WorkflowStep(5, "Check result", action_type="assert_clipboard", value="42"),
        )
        with patch(
            "gpa.execution.safe_web.fetch_public_page",
            return_value=("https://example.test/report", "Verified fact", 200),
        ):
            result = SafeWebRunner(workflow, variables={"answer": "42"}).run()

        self.assertTrue(result.success)
        self.assertEqual(result.execution_mode, "safe_web")
        self.assertEqual(result.n_steps, 5)
        self.assertEqual(
            [item.evidence_source for item in result.step_results],
            ["public-http", "public-http", "public-http", "run-memory", "run-memory"],
        )

    def test_runner_can_verify_a_real_link_target_without_navigating_to_it(self):
        workflow = safe_workflow(
            WorkflowStep(1, "Open source", action_type="open_url", value="https://example.test/report"),
            WorkflowStep(2, "Check file link", action_type="assert_link", value="ftp://files.test/data/"),
        )
        with patch(
            "gpa.execution.safe_web.fetch_public_page",
            return_value=(
                "https://example.test/report",
                "Published files",
                200,
                ["ftp://files.test/data/", "https://example.test/about"],
            ),
        ):
            result = SafeWebRunner(workflow).run()

        self.assertTrue(result.success)
        self.assertEqual(result.step_results[1].evidence_source, "public-http")

    def test_http_failure_is_sanitized_to_status_and_host(self):
        workflow = safe_workflow(
            WorkflowStep(1, "Open source", action_type="open_url", value="https://example.test/secret?token=nope"),
        )
        error = HTTPError(
            "https://example.test/secret?token=nope",
            403,
            "Forbidden",
            hdrs=None,
            fp=None,
        )
        with patch("gpa.execution.safe_web.fetch_public_page", side_effect=error):
            result = SafeWebRunner(workflow).run()

        self.assertFalse(result.success)
        self.assertEqual(result.n_failed, 1)
        self.assertEqual(result.error, "Public source returned HTTP 403: example.test")
        self.assertNotIn("token", result.error)

    def test_transient_incomplete_response_is_retried(self):
        workflow = safe_workflow(
            WorkflowStep(1, "Open source", action_type="open_url", value="https://example.test/report"),
            WorkflowStep(2, "Check source", action_type="assert_text", value="Recovered evidence"),
        )
        with patch(
            "gpa.execution.safe_web.fetch_public_page",
            side_effect=[
                IncompleteRead(b"partial"),
                ("https://example.test/report", "Recovered evidence", 200),
            ],
        ) as fetch:
            result = SafeWebRunner(workflow).run()

        self.assertTrue(result.success)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result.step_results[0].retries, 1)

    def test_runner_honors_cancellation_before_any_step(self):
        workflow = safe_workflow(
            WorkflowStep(1, "Open source", action_type="open_url", value="https://example.com"),
        )
        with patch("gpa.execution.safe_web.fetch_public_page") as fetch:
            result = SafeWebRunner(workflow, should_stop=lambda: True).run()
        self.assertFalse(result.success)
        self.assertEqual(result.n_steps, 0)
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
