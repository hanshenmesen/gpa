import unittest
from unittest.mock import patch

from gpa.replay.environment import capture_environment, compare_environments
from gpa.replay.understanding import build_agent_understanding, build_reproduction_contract
from gpa.storage.workflow import Workflow, WorkflowStep


class ReplayEnvironmentTests(unittest.TestCase):
    def test_agent_understanding_carries_semantic_plan_and_mutation_risk(self):
        workflow = Workflow(
            workflow_id="portable-plan",
            workflow_name="portable-plan",
            workflow_title="Portable plan",
            description="Update and verify a published item.",
            task_description="Update a published item safely.",
            steps=[
                WorkflowStep(1, "Open the item", action_type="open_url", value="https://example.com/item"),
                WorkflowStep(2, "Save the updated item", action_type="click", active_app_name="Chrome"),
                WorkflowStep(3, "Verify saved", action_type="assert_text", value="Saved"),
            ],
        )

        result = build_agent_understanding(workflow)

        self.assertEqual(len(result["semantic_plan"]), 3)
        self.assertEqual(result["semantic_plan"][0]["phase"], "navigate")
        self.assertEqual(result["semantic_plan"][2]["phase"], "verify")
        self.assertFalse(result["risk_controls"]["read_only"])
        self.assertTrue(result["risk_controls"]["requires_explicit_arm"])
        self.assertEqual(result["risk_controls"]["mutation_signals"][0]["step"], 2)

    def test_capture_combines_host_and_browser_context(self):
        with patch.dict("os.environ", {"GPA_ENABLE_DESKTOP_AUTOMATION": "0"}, clear=False):
            result = capture_environment({
                "language": "zh-CN",
                "timezone": "Asia/Shanghai",
                "screen": {"width": 1512, "height": 982, "pixel_ratio": 2},
                "browser": {
                    "family": "Chrome",
                    "user_agent": "fixture-agent",
                    "viewport_width": 1200,
                    "viewport_height": 800,
                },
            })

        self.assertEqual(result["schema"], "gpa.environment/v1")
        self.assertEqual(result["locale"]["language"], "zh-CN")
        self.assertEqual(result["screen"]["width"], 1512)
        self.assertEqual(result["browser"]["family"], "Chrome")
        self.assertFalse(result["input_safety"]["desktop_automation_enabled"])
        self.assertFalse(result["input_safety"]["input_watchdog_enabled"])

    def test_compare_reports_scale_hint_and_blocking_platform_change(self):
        recorded = {
            "system": {"name": "darwin", "machine": "arm64"},
            "browser": {"family": "Chrome"},
            "screen": {"width": 1000, "height": 800},
        }
        current = {
            "system": {"name": "windows", "machine": "amd64"},
            "browser": {"family": "Edge"},
            "screen": {"width": 1600, "height": 1200},
        }

        result = compare_environments(recorded, current)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["blocking_count"], 1)
        self.assertGreaterEqual(result["warning_count"], 2)
        self.assertFalse(result["safe_to_attempt"])
        self.assertTrue(result["requires_replan"])
        dimensions = next(item for item in result["differences"] if item["field"] == "screen.dimensions")
        self.assertEqual(dimensions["scale_hint"], {"x": 1.6, "y": 1.5})
        screen_plan = next(item for item in result["adaptation_plan"] if item["field"] == "screen.dimensions")
        self.assertEqual(screen_plan["strategy"], "scale_then_relocalize")
        self.assertEqual(screen_plan["scale_hint"], {"x": 1.6, "y": 1.5})

    def test_compare_treats_missing_recorded_identity_as_unknown(self):
        current = capture_environment()

        result = compare_environments({}, current)

        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["recorded_environment_known"])
        self.assertTrue(result["current_environment_known"])
        self.assertFalse(result["evidence_complete"])
        self.assertEqual(result["missing_evidence"], ["recorded.system.name"])
        self.assertFalse(result["safe_to_attempt"])
        self.assertTrue(result["requires_replan"])

    def test_compare_treats_missing_current_identity_as_unknown(self):
        recorded = {"system": {"name": "darwin", "machine": "arm64"}}

        result = compare_environments(recorded, {})

        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["recorded_environment_known"])
        self.assertFalse(result["current_environment_known"])
        self.assertEqual(result["missing_evidence"], ["current.system.name"])
        self.assertFalse(result["safe_to_attempt"])

    def test_compare_accepts_matching_platform_with_sparse_optional_evidence(self):
        recorded = {"system": {"name": "darwin"}}
        current = {"system": {"name": "darwin"}}

        result = compare_environments(recorded, current)

        self.assertEqual(result["status"], "compatible")
        self.assertTrue(result["evidence_complete"])
        self.assertTrue(result["safe_to_attempt"])

    def test_reproduction_gate_preserves_explicit_empty_target_environment(self):
        from demo_web.server import _workflow_reproduction_gate

        workflow = Workflow(
            workflow_id="unknown-target",
            workflow_name="unknown-target",
            workflow_title="Unknown target",
            description="Check unknown target handling.",
            environment={"system": {"name": "darwin", "machine": "arm64"}},
            steps=[WorkflowStep(1, "Wait", action_type="wait", value="1")],
        )

        gate = _workflow_reproduction_gate(workflow, {}, {})

        self.assertEqual(gate["environment_diff"]["status"], "unknown")
        self.assertFalse(gate["environment_diff"]["current_environment_known"])

    def test_compare_reports_viewport_pixel_ratio_and_input_authority_changes(self):
        recorded = {
            "system": {"name": "darwin", "machine": "arm64"},
            "screen": {"width": 1512, "height": 982, "pixel_ratio": 2},
            "browser": {"family": "Chrome", "viewport_width": 1400, "viewport_height": 800},
            "input_safety": {"desktop_automation_enabled": True},
        }
        current = {
            "system": {"name": "darwin", "machine": "arm64"},
            "screen": {"width": 1512, "height": 982, "pixel_ratio": 1},
            "browser": {"family": "Chrome", "viewport_width": 900, "viewport_height": 700},
            "input_safety": {"desktop_automation_enabled": False},
        }

        result = compare_environments(recorded, current)
        fields = {item["field"] for item in result["differences"]}
        strategies = {item["strategy"] for item in result["adaptation_plan"]}

        self.assertIn("browser.viewport", fields)
        self.assertIn("screen.pixel_ratio", fields)
        self.assertIn("input_safety.desktop_automation_enabled", fields)
        self.assertIn("responsive_relocalization", strategies)
        self.assertIn("pixel_ratio_normalization", strategies)
        self.assertIn("execution_mode_replan", strategies)

    def test_reproduction_contract_requires_real_handoff_evidence(self):
        environment = {
            "system": {"name": "darwin", "machine": "arm64"},
            "screen": {"width": 1512, "height": 982},
        }
        understanding = {
            "schema": "gpa.agent-understanding/v1",
            "goal": "Verify a published result",
            "required_environment": {"applications": ["Chrome"], "web_hosts": ["example.com"]},
            "interaction_profile": {"step_count": 2},
            "success_criteria": [{"step": 2, "type": "assert_text", "expected": "Published"}],
            "risk_controls": {"read_only": True},
        }
        artifacts = {
            "recording": {
                "path": "recording.mp4",
                "mime_type": "video/mp4",
                "bytes": 4096,
                "sha256": "a" * 64,
                "duration_seconds": 3.5,
                "width": 1512,
                "height": 982,
                "capture_scope": "browser-tab",
                "capture_method": "browser-tab-frame-capture",
                "privacy_review": {
                    "status": "passed",
                    "other_apps_visible": False,
                    "scope_confirmed": "browser-tab-only",
                },
            }
        }
        environment_diff = compare_environments(
            environment,
            {
                "system": {"name": "windows", "machine": "amd64"},
                "screen": {"width": 1920, "height": 1080},
            },
        )
        environment_diff["current_environment_known"] = True

        ready = build_reproduction_contract(
            step_count=2,
            environment=environment,
            understanding=understanding,
            artifacts=artifacts,
            environment_diff=environment_diff,
            recording_verified=True,
        )
        incomplete = build_reproduction_contract(
            step_count=2,
            environment=environment,
            understanding=understanding,
            artifacts={},
            environment_diff=environment_diff,
            recording_verified=False,
        )

        self.assertEqual(ready["schema"], "gpa.reproduction-contract/v1")
        self.assertEqual(ready["status"], "adaptation_required")
        self.assertEqual(ready["score"], 100)
        self.assertTrue(ready["publishable_as_verified"])
        self.assertIn("platform_replan", {
            item["strategy"] for item in ready["handoff"]["adaptation_plan"]
        })
        self.assertEqual(incomplete["status"], "incomplete")
        self.assertIn("recording_evidence", incomplete["blockers"])
        self.assertIn("recording_privacy", incomplete["blockers"])

    def test_reproduction_contract_honors_explicit_unknown_current_environment(self):
        contract = build_reproduction_contract(
            step_count=1,
            environment={
                "system": {"name": "darwin", "machine": "arm64"},
                "screen": {"width": 1512, "height": 982},
            },
            understanding={
                "schema": "gpa.agent-understanding/v1",
                "goal": "Verify a public source",
                "interaction_profile": {"step_count": 1},
                "success_criteria": [{"step": 1, "type": "assert_text"}],
            },
            artifacts={},
            environment_diff={
                "status": "unknown",
                "current_environment_known": False,
                "matches": ["browser.family"],
                "differences": [],
            },
            recording_verified=False,
        )

        self.assertIn("current_environment_unknown", contract["warnings"])

    def test_reproduction_contract_rejects_decodable_full_screen_recording_with_other_apps(self):
        contract = build_reproduction_contract(
            step_count=1,
            environment={
                "system": {"name": "darwin", "machine": "arm64"},
                "screen": {"width": 1512, "height": 982},
            },
            understanding={
                "schema": "gpa.agent-understanding/v1",
                "goal": "Verify a public source",
                "interaction_profile": {"step_count": 1},
                "success_criteria": [{"step": 1, "type": "assert_text"}],
            },
            artifacts={
                "recording": {
                    "path": "recording.mp4",
                    "mime_type": "video/mp4",
                    "bytes": 4096,
                    "sha256": "a" * 64,
                    "capture_scope": "monitor",
                    "privacy_review": {
                        "status": "failed",
                        "other_apps_visible": True,
                        "scope_confirmed": "monitor",
                    },
                }
            },
            recording_verified=True,
        )

        self.assertFalse(contract["publishable_as_verified"])
        self.assertIn("recording_privacy", contract["blockers"])
        self.assertTrue(next(
            item["passed"] for item in contract["checks"]
            if item["id"] == "recording_evidence"
        ))

    def test_reproduction_contract_accepts_verified_browser_viewport_as_capture_environment(self):
        contract = build_reproduction_contract(
            step_count=1,
            environment={
                "system": {"name": "darwin", "machine": "arm64"},
                "screen": {"width": 0, "height": 0},
                "browser": {
                    "family": "Codex In-app Browser",
                    "viewport_width": 1280,
                    "viewport_height": 720,
                },
            },
            understanding={
                "schema": "gpa.agent-understanding/v1",
                "goal": "Verify a public source",
                "interaction_profile": {"step_count": 1},
                "success_criteria": [{"step": 1, "type": "assert_text"}],
            },
            artifacts={
                "recording": {
                    "path": "recording.mp4",
                    "mime_type": "video/mp4",
                    "bytes": 4096,
                    "sha256": "b" * 64,
                    "capture_scope": "browser-tab",
                    "privacy_review": {
                        "status": "passed",
                        "other_apps_visible": False,
                        "scope_confirmed": "browser-tab",
                    },
                }
            },
            environment_diff={"current_environment_known": True},
            recording_verified=True,
        )

        environment_check = next(
            item for item in contract["checks"] if item["id"] == "recorded_environment"
        )
        self.assertTrue(environment_check["passed"])
        self.assertEqual(
            contract["handoff"]["recorded_capture_dimensions"],
            {"source": "browser.viewport", "width": 1280, "height": 720},
        )

    def test_browser_tab_capture_uses_decoded_surface_and_ignores_monitor_drift(self):
        recorded = {
            "system": {"name": "darwin", "machine": "arm64"},
            "screen": {"width": 2940, "height": 1912, "pixel_ratio": 2},
            "browser": {"family": "Chrome", "viewport_width": 1280, "viewport_height": 720},
            "capture_surface": {
                "scope": "browser-tab", "width": 1280, "height": 720,
            },
        }
        current = {
            "system": {"name": "darwin", "machine": "arm64"},
            "screen": {"width": 1470, "height": 956, "pixel_ratio": 2},
            "browser": {"family": "Chrome", "viewport_width": 1280, "viewport_height": 720},
        }

        diff = compare_environments(recorded, current)

        self.assertNotIn("screen.dimensions", {
            item["field"] for item in diff["differences"]
        })
        self.assertIn("browser.viewport", diff["matches"])

        contract = build_reproduction_contract(
            step_count=1,
            environment=recorded,
            understanding={
                "schema": "gpa.agent-understanding/v1",
                "goal": "Verify a public source",
                "interaction_profile": {"step_count": 1},
                "success_criteria": [{"step": 1, "type": "assert_text"}],
            },
            artifacts={
                "recording": {
                    "path": "recording.mp4",
                    "mime_type": "video/mp4",
                    "bytes": 4096,
                    "sha256": "c" * 64,
                    "capture_scope": "browser-tab",
                    "privacy_review": {
                        "status": "passed",
                        "other_apps_visible": False,
                        "scope_confirmed": "browser-tab",
                    },
                }
            },
            environment_diff=diff,
            recording_verified=True,
        )
        self.assertEqual(
            contract["handoff"]["recorded_capture_dimensions"],
            {"source": "capture_surface", "width": 1280, "height": 720},
        )

    def test_public_web_evidence_requires_matching_machine_readable_source_trace(self):
        environment = {
            "system": {"name": "darwin", "machine": "arm64"},
            "capture_surface": {
                "scope": "public-web-evidence", "width": 1280, "height": 720,
            },
        }
        understanding = {
            "schema": "gpa.agent-understanding/v1",
            "goal": "Verify ten public weather pages",
            "interaction_profile": {"step_count": 1},
            "success_criteria": [{"step": 1, "type": "assert_text"}],
        }
        recording = {
            "path": "recording.mp4",
            "mime_type": "video/mp4",
            "bytes": 4096,
            "sha256": "d" * 64,
            "capture_scope": "public-web-evidence",
            "source_run_id": "run-1",
            "privacy_review": {
                "status": "passed",
                "other_apps_visible": False,
                "scope_confirmed": "public-web-evidence",
            },
        }
        missing = build_reproduction_contract(
            step_count=1,
            environment=environment,
            understanding=understanding,
            artifacts={"recording": recording},
            environment_diff={"current_environment_known": True},
            recording_verified=True,
        )
        self.assertIn("source_trace_evidence", missing["blockers"])

        verified = build_reproduction_contract(
            step_count=1,
            environment=environment,
            understanding=understanding,
            artifacts={
                "recording": recording,
                "source_trace": {
                    "path": "source_trace.json",
                    "schema": "gpa.safe-web-source-trace/v1",
                    "bytes": 1024,
                    "sha256": "e" * 64,
                    "source_run_id": "run-1",
                    "page_count": 10,
                    "verified_page_count": 10,
                },
            },
            environment_diff={"current_environment_known": True},
            recording_verified=True,
        )
        self.assertTrue(verified["publishable_as_verified"])
        self.assertTrue(verified["handoff"]["source_trace_verified"])
        self.assertEqual(verified["handoff"]["source_trace_page_count"], 10)


if __name__ == "__main__":
    unittest.main()
