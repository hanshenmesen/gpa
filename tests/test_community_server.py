import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import demo_web.server as server
import gpa.storage.workflow as workflow_module
from gpa.storage.workflow import Workflow, WorkflowStep, WorkflowStorage


class DummyHandler:
    def __init__(self, payload=None, *, origin=""):
        raw = json.dumps(payload or {}).encode("utf-8")
        self.headers = {"Content-Length": str(len(raw))}
        if origin:
            self.headers["Origin"] = origin
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass

    def json(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


class CommunityServerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.old_workflows_dir = server.WORKFLOWS_DIR
        self.old_community_dir = server.COMMUNITY_DIR
        self.old_maintained_community_workflows = server.MAINTAINED_COMMUNITY_WORKFLOWS
        self.old_module_workflows_dir = workflow_module.WORKFLOWS_DIR
        server.WORKFLOWS_DIR = root / "workflows"
        server.COMMUNITY_DIR = root / "community"
        workflow_module.WORKFLOWS_DIR = server.WORKFLOWS_DIR
        server.WORKFLOWS_DIR.mkdir()
        WorkflowStorage().save(
            Workflow(
                workflow_id="publish_me",
                workflow_name="publish_me",
                workflow_title="Publish Me",
                description="Community API fixture.",
                steps=[WorkflowStep(1, "Click", action_type="click")],
            ),
            {},
        )

    def tearDown(self):
        server.WORKFLOWS_DIR = self.old_workflows_dir
        server.COMMUNITY_DIR = self.old_community_dir
        workflow_module.WORKFLOWS_DIR = self.old_module_workflows_dir
        server.MAINTAINED_COMMUNITY_WORKFLOWS = self.old_maintained_community_workflows
        self._tmp.cleanup()

    def publish(self):
        handler = DummyHandler(
            {
                "workflow_id": "publish_me",
                "author": "Tester",
                "tags": ["demo"],
                "record_license": "CC-BY-4.0",
                "privacy_reviewed": True,
            }
        )
        server._publish_community_record(handler)
        self.assertEqual(handler.status, 201)
        return handler.json()["record"]

    def test_publish_import_and_feedback_api_helpers(self):
        record = self.publish()

        feedback = DummyHandler({"success": True, "note": "works"})
        server._submit_community_feedback(feedback, record["record_id"])
        self.assertEqual(feedback.status, 201)

        imported = DummyHandler({
            "workflow_id": "imported_from_community",
            "client_environment": {
                "language": "zh-CN",
                "timezone": "Asia/Shanghai",
                "screen": {"width": 1512, "height": 982, "pixel_ratio": 2},
                "browser": {
                    "family": "Google Chrome",
                    "viewport_width": 1280,
                    "viewport_height": 760,
                },
            },
        })
        with patch.object(server, "_start_replay") as start_replay:
            server._import_community_record(imported, record["record_id"])
            start_replay.assert_not_called()
        self.assertEqual(imported.status, 201)
        self.assertEqual(imported.json()["workflow_id"], "imported_from_community")
        self.assertFalse(imported.json()["already_saved"])
        gate = imported.json()["workflow"]["reproduction_gate"]
        self.assertEqual(gate["schema"], "gpa.reproduction-gate/v1")
        self.assertTrue(gate["environment_diff"]["current_environment_known"])
        self.assertTrue(gate["decision_id"])
        self.assertFalse(server.STATE["run"]["active"])

        repeated = DummyHandler({"workflow_id": "another_copy"})
        server._import_community_record(repeated, record["record_id"])
        self.assertEqual(repeated.status, 200)
        self.assertTrue(repeated.json()["already_saved"])
        self.assertEqual(repeated.json()["workflow_id"], "imported_from_community")

        detail = server._community_repository().get_record(record["record_id"], include_feedback=True)
        self.assertEqual(detail["stats"]["imports"], 1)
        self.assertEqual(detail["stats"]["feedback_count"], 1)

    def test_publish_enriches_workflow_with_console_environment_and_understanding(self):
        handler = DummyHandler({
            "workflow_id": "publish_me",
            "author": "Tester",
            "privacy_reviewed": True,
            "client_environment": {
                "language": "zh-CN",
                "timezone": "Asia/Shanghai",
                "screen": {"width": 1512, "height": 982, "pixel_ratio": 2},
                "browser": {
                    "family": "Google Chrome",
                    "viewport_width": 1280,
                    "viewport_height": 760,
                },
            },
        })

        server._publish_community_record(handler)

        self.assertEqual(handler.status, 201)
        workflow, _ = WorkflowStorage().load("publish_me")
        self.assertEqual(workflow.environment["browser"]["family"], "Google Chrome")
        self.assertEqual(workflow.environment["browser"]["viewport_width"], 1280)
        self.assertEqual(workflow.environment["locale"]["language"], "zh-CN")
        self.assertEqual(workflow.understanding["schema"], "gpa.agent-understanding/v1")
        record = handler.json()["record"]
        self.assertTrue(record["environment"])
        self.assertTrue(record["understanding"])

    def test_non_benchmark_plugin_receives_runtime_reproduction_status(self):
        run = {
            "run_id": "run-uploaded-plugin",
            "workflow_id": "uploaded_plugin",
            "success": True,
            "steps_run": 7,
            "finished_at": "2026-08-10T18:00:00Z",
            "steps": [{"evidence_source": "public-http"}],
        }

        enriched = server._enrich_community_reproduction(
            {"workflow_id": "uploaded_plugin", "tags": ["community-plugin"]},
            {"uploaded_plugin": run},
        )

        self.assertEqual(enriched["reproduction"]["status"], "succeeded")
        self.assertEqual(enriched["reproduction"]["steps_run"], 7)
        self.assertEqual(enriched["reproduction"]["evidence_sources"], ["public-http"])

    def test_isolated_audit_is_persisted_without_touching_desktop_input(self):
        record = self.publish()
        audit_report = {
            "schema": "gpa.isolated-reproduction-audit/v1",
            "status": "passed",
            "cross_agent_reproducible": True,
            "package": {"sha256": record["package_sha256"]},
            "isolation": {"separate_workflow_repository": True},
            "recording": {
                "verified": True,
                "media_verified": True,
                "source_run_id": "source-run-7",
            },
            "target_environment": {"system": {"name": "darwin", "machine": "arm64"}},
            "environment_diff": {"status": "degraded", "adaptation_plan": [{"field": "screen.dimensions"}]},
            "reproduction_contract": {"score": 100, "status": "adaptation_required"},
            "execution": {
                "attempted": True,
                "success": True,
                "mode": "safe_web",
                "desktop_input": False,
                "steps_run": 7,
                "steps_failed": 0,
                "semantic_assertions_verified": 7,
                "elapsed_seconds": 2.4,
                "evidence_sources": ["public-http"],
                "error": "",
            },
        }
        handler = DummyHandler({
            "client_environment": {
                "screen": {"width": 1512, "height": 982, "pixel_ratio": 2},
                "browser": {"family": "Google Chrome"},
            }
        })

        with patch.object(
            server,
            "_run_isolated_audit_worker",
            return_value=audit_report,
        ), patch.object(server, "_start_replay") as start_replay:
            server._audit_community_record_isolated(handler, record["record_id"])

        self.assertEqual(handler.status, 200)
        start_replay.assert_not_called()
        summary = handler.json()["summary"]
        self.assertTrue(summary["cross_agent_reproducible"])
        self.assertTrue(summary["worker_process_isolated"])
        self.assertTrue(summary["recording_media_verified"])
        self.assertEqual(summary["recording_source_run_id"], "source-run-7")
        self.assertFalse(summary["execution"]["desktop_input"])
        stored = server._community_repository().get_record(record["record_id"])
        self.assertEqual(stored["isolated_reproduction_audit"]["status"], "passed")
        self.assertFalse(server.STATE["isolated_reproduction_audit"]["active"])

    def test_isolated_audit_worker_strips_model_credentials_and_desktop_access(self):
        report = {"schema": "gpa.isolated-reproduction-audit/v1", "status": "passed"}
        completed = server.subprocess.CompletedProcess(
            ["python", "-m", "gpa.replay.audit_worker"],
            0,
            stdout=json.dumps(report),
            stderr="",
        )
        with patch.dict(
            server.os.environ,
            {
                "GPA_LLM_API_KEY": "must-not-leak",
                "OPENAI_API_KEY": "must-not-leak",
                "GPA_ENABLE_DESKTOP_AUTOMATION": "1",
            },
            clear=False,
        ), patch.object(server.subprocess, "run", return_value=completed) as run:
            result = server._run_isolated_audit_worker(
                Path("/tmp/fixture.gpa-record.zip"),
                {"schema": "gpa.environment/v1"},
            )

        self.assertEqual(result, report)
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["env"]["GPA_ENABLE_DESKTOP_AUTOMATION"], "0")
        self.assertEqual(kwargs["env"]["GPA_ENABLE_INPUT_WATCHDOG"], "0")
        self.assertNotIn("GPA_LLM_API_KEY", kwargs["env"])
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertIn("gpa.replay.audit_worker", run.call_args.args[0])

    def test_isolated_audit_worker_timeout_is_reported_as_controlled_failure(self):
        timeout = server.subprocess.TimeoutExpired(
            ["python", "-m", "gpa.replay.audit_worker"],
            server.ISOLATED_AUDIT_TIMEOUT_SECONDS,
        )
        with patch.object(server.subprocess, "run", side_effect=timeout):
            with self.assertRaisesRegex(RuntimeError, "exceeded .* and was terminated"):
                server._run_isolated_audit_worker(
                    Path("/tmp/fixture.gpa-record.zip"),
                    {"schema": "gpa.environment/v1"},
                )

    def test_recording_decoder_isolated_worker_strips_credentials_and_desktop_access(self):
        report = {
            "schema": "gpa.recording-media-probe/v1",
            "status": "verified",
            "verified": True,
            "decoded_sample_count": 3,
        }
        completed = server.subprocess.CompletedProcess(
            ["python", "-m", "gpa.community.media_probe"],
            0,
            stdout=json.dumps(report),
            stderr="",
        )
        with patch.dict(
            server.os.environ,
            {"GPA_LLM_API_KEY": "must-not-leak", "OPENAI_API_KEY": "must-not-leak"},
            clear=False,
        ), patch.object(server.subprocess, "run", return_value=completed) as run:
            result = server._run_isolated_media_probe(Path("/tmp/recording.mp4"))

        self.assertTrue(result["verified"])
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["env"]["GPA_ENABLE_DESKTOP_AUTOMATION"], "0")
        self.assertNotIn("GPA_LLM_API_KEY", kwargs["env"])
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertIn("gpa.community.media_probe", run.call_args.args[0])

    def test_recording_decoder_timeout_cannot_block_server_process(self):
        timeout = server.subprocess.TimeoutExpired(
            ["python", "-m", "gpa.community.media_probe"],
            server.ISOLATED_MEDIA_PROBE_TIMEOUT_SECONDS,
        )
        with patch.object(server.subprocess, "run", side_effect=timeout):
            result = server._run_isolated_media_probe(Path("/tmp/recording.mp4"))

        self.assertEqual(result["status"], "timeout")
        self.assertFalse(result["verified"])

    def test_verified_publication_contract_requires_decodable_recording(self):
        manifest = {
            "workflow_id": "portable",
            "workflow_name": "portable",
            "workflow_title": "Portable",
            "step_count": 1,
            "environment": {
                "schema": "gpa.environment/v1",
                "system": {"name": "darwin", "machine": "arm64"},
                "screen": {"width": 1280, "height": 832},
            },
            "understanding": {
                "schema": "gpa.agent-understanding/v1",
                "goal": "Verify a public source",
                "interaction_profile": {"step_count": 1},
                "success_criteria": [
                    {"step": 1, "type": "assert_text", "expected": "verified"}
                ],
            },
            "artifacts": {
                "recording": {
                    "path": "recording.mp4",
                    "mime_type": "video/mp4",
                    "bytes": 4096,
                    "sha256": "a" * 64,
                    "duration_seconds": 3.0,
                    "width": 1280,
                    "height": 832,
                    "capture_scope": "browser-tab",
                    "capture_method": "browser-tab-frame-capture",
                    "privacy_review": {
                        "status": "passed",
                        "other_apps_visible": False,
                        "scope_confirmed": "browser-tab",
                    },
                }
            },
        }
        current = {
            "system": {"name": "darwin", "machine": "arm64"},
            "screen": {"width": 1280, "height": 832},
        }

        invalid = server._community_package_inspection(
            manifest,
            4096,
            current_environment=current,
            recording_probe={
                "schema": "gpa.recording-media-probe/v1",
                "status": "invalid",
                "verified": False,
            },
        )
        verified = server._community_package_inspection(
            manifest,
            4096,
            current_environment=current,
            recording_probe={
                "schema": "gpa.recording-media-probe/v1",
                "status": "verified",
                "verified": True,
                "decoded_sample_count": 3,
            },
        )

        self.assertFalse(invalid["reproduction_contract"]["publishable_as_verified"])
        self.assertIn("recording_evidence", invalid["reproduction_contract"]["blockers"])
        self.assertTrue(verified["reproduction_contract"]["publishable_as_verified"])
        self.assertTrue(verified["evidence"]["recording_media_verified"])

    def test_isolated_audit_rejects_concurrent_worker_without_starting_another(self):
        handler = DummyHandler()
        previous = dict(server.STATE.get("isolated_reproduction_audit") or {})
        server.STATE["isolated_reproduction_audit"] = {
            "active": True,
            "record_id": "rec_active",
        }
        try:
            with patch.object(server, "_run_isolated_audit_worker") as worker:
                server._audit_community_record_isolated(handler, "rec_second")
            worker.assert_not_called()
            self.assertEqual(handler.status, 409)
            self.assertIn("rec_active", handler.json()["error"])
        finally:
            server.STATE["isolated_reproduction_audit"] = previous

    def test_publish_rejects_missing_privacy_confirmation(self):
        handler = DummyHandler({"workflow_id": "publish_me", "privacy_reviewed": False})

        server._publish_community_record(handler)

        self.assertEqual(handler.status, 422)
        self.assertEqual(server._community_repository().list_records(), [])

    def test_evidence_repair_preserves_richer_local_recording_metadata(self):
        storage = WorkflowStorage()
        workflow, subgraphs = storage.load("publish_me")
        recording = b"\x00\x00\x00\x18ftypmp42recording"
        digest = hashlib.sha256(recording).hexdigest()
        workflow.artifacts = {
            "recording": {
                "kind": "screen-recording",
                "path": "recording.mp4",
                "mime_type": "video/mp4",
                "bytes": len(recording),
                "sha256": digest,
                "duration_seconds": 12.5,
                "width": 1280,
                "height": 832,
                "run_id": "run-real-evidence",
            }
        }
        storage.save(workflow, subgraphs)
        (workflow.storage_dir / "recording.mp4").write_bytes(recording)
        stale_repository = unittest.mock.Mock()
        stale_repository.list_records.return_value = [{
            "workflow_id": "publish_me",
            "artifacts": {
                "recording": {
                    "kind": "screen-recording",
                    "path": "recording.mp4",
                    "mime_type": "video/mp4",
                    "bytes": len(recording),
                    "sha256": digest,
                }
            },
        }]

        with patch.object(server, "_community_repository", return_value=stale_repository):
            server._repair_local_workflow_evidence()

        repaired, _ = storage.load("publish_me")
        evidence = repaired.artifacts["recording"]
        self.assertEqual(evidence["duration_seconds"], 12.5)
        self.assertEqual((evidence["width"], evidence["height"]), (1280, 832))
        self.assertEqual(evidence["run_id"], "run-real-evidence")

    def test_demo_records_are_real_idempotent_packages_without_local_workflow_pollution(self):
        first = server._ensure_demo_community_records()
        records = server._community_repository().list_records()

        self.assertEqual(len(first), 12)
        self.assertEqual(len(records), 12)
        self.assertTrue(all({"demo", "case", "tutorial"} & set(record["tags"]) for record in records))
        self.assertEqual(
            {record["workflow_id"] for record in records},
            {
                "case_order_wrong_state",
                "case_order_flaky_retry",
                "case_order_dynamic_layout",
                "case_order_validation_repair",
                "demo_web_research",
                "demo_project_dashboard",
                "demo_meeting_prep",
                "demo_daily_brief",
                "tutorial_gmail_filter",
                "tutorial_sheets_filter_view",
                "tutorial_excel_dropdown",
                "tutorial_macos_shortcut",
            },
        )
        self.assertFalse((server.COMMUNITY_DIR / ".demo-seed").exists())
        self.assertEqual(workflow_module.WORKFLOWS_DIR, server.WORKFLOWS_DIR)
        self.assertEqual(
            {path.name for path in server.WORKFLOWS_DIR.iterdir()},
            {"publish_me"},
        )

        second = server._ensure_demo_community_records()
        self.assertTrue(all(record["duplicate"] for record in second))
        self.assertEqual(len(server._community_repository().list_records()), 12)

        selected = next(record for record in records if record["workflow_id"] == "demo_web_research")
        imported = server._community_repository().import_record(
            selected["record_id"],
            workflow_id="saved_demo",
            storage=WorkflowStorage(),
        )
        workflow, _ = WorkflowStorage().load(imported.workflow_id)
        self.assertEqual(workflow.workflow_id, "saved_demo")
        self.assertEqual(workflow.variables[0].name, "query")
        self.assertEqual(workflow.steps[0].action_type, "open_url")

        case_record = next(
            record for record in records if record["workflow_id"] == "case_order_wrong_state"
        )
        case_import = server._community_repository().import_record(
            case_record["record_id"],
            workflow_id="saved_case",
            storage=WorkflowStorage(),
        )
        case_workflow, case_subgraphs = WorkflowStorage().load(case_import.workflow_id)
        self.assertEqual(len(case_workflow.steps), 12)
        self.assertEqual(len(case_subgraphs), 7)
        self.assertTrue(
            server._workflow_quality_payload(case_workflow, case_subgraphs)["runnable"]
        )
        self.assertIn("/case-lab?mode=wrong", case_workflow.steps[0].value)

        tutorial_record = next(
            record for record in records if record["workflow_id"] == "tutorial_excel_dropdown"
        )
        tutorial = tutorial_record["provenance"]["tutorial_source"]
        self.assertEqual(tutorial["publisher"], "Microsoft")
        self.assertEqual(tutorial_record["provenance"]["practice"]["mode"], "isolated-local-lab")
        self.assertFalse(tutorial_record["provenance"]["practice"]["external_writes"])
        self.assertIn("support.microsoft.com", tutorial["url"])
        imported_tutorial = server._community_repository().import_record(
            tutorial_record["record_id"],
            workflow_id="saved_tutorial",
            storage=WorkflowStorage(),
        )
        tutorial_workflow, tutorial_subgraphs = WorkflowStorage().load(
            imported_tutorial.workflow_id
        )
        self.assertEqual(tutorial_workflow.steps[-1].action_type, "assert_text")
        self.assertEqual(tutorial_workflow.steps[-1].value, "数据验证已生效")
        self.assertGreaterEqual(len(tutorial_subgraphs), 6)

    def test_publish_rejects_oversized_body_without_reading_it(self):
        class NoRead(io.BytesIO):
            def read(self, *args, **kwargs):
                raise AssertionError("oversized request body must not be read")

        handler = DummyHandler()
        handler.headers["Content-Length"] = str(server.COMMUNITY_MAX_JSON_BYTES + 1)
        handler.rfile = NoRead(b"ignored")

        server._publish_community_record(handler)

        self.assertEqual(handler.status, 413)

    def test_streamed_request_rejects_oversize_and_short_body_without_leaking_temp_files(self):
        uploads = server.COMMUNITY_DIR / ".uploads"

        class NoRead(io.BytesIO):
            def read(self, *args, **kwargs):
                raise AssertionError("oversized request body must not be read")

        oversized = DummyHandler()
        oversized.headers["Content-Length"] = str(server.COMMUNITY_MAX_PACKAGE_BYTES + 1)
        oversized.rfile = NoRead(b"ignored")
        with self.assertRaises(server.PayloadTooLargeError):
            server._read_request_to_temp(
                oversized,
                max_bytes=server.COMMUNITY_MAX_PACKAGE_BYTES,
                directory=uploads,
                suffix=".zip",
            )

        short = DummyHandler()
        short.headers["Content-Length"] = "10"
        short.rfile = io.BytesIO(b"short")
        with self.assertRaisesRegex(ValueError, "ended early"):
            server._read_request_to_temp(
                short,
                max_bytes=server.COMMUNITY_MAX_PACKAGE_BYTES,
                directory=uploads,
                suffix=".zip",
            )
        self.assertFalse(any(uploads.glob(".upload-*")))

    def test_maintained_local_recording_is_disclosed_as_internal_regression(self):
        WorkflowStorage().save(
            Workflow(
                workflow_id="github_dual_code_audit",
                workflow_name="github_dual_code_audit",
                workflow_title="GitHub cross-file audit",
                description="A maintained read-only browser regression.",
                task_description="Inspect two source files in a public GitHub repository.",
                steps=[
                    WorkflowStep(
                        1,
                        "Open the public repository",
                        action_type="open_url",
                        value="https://github.com/hanshenmesen/gpa",
                        active_app_name="Google Chrome",
                    )
                ],
            ),
            {},
        )

        first = server._ensure_local_real_community_records()
        records = server._community_repository().list_records(tag="internal-regression")

        self.assertEqual(len(first), 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["workflow_id"], "github_dual_code_audit")
        self.assertIn("internal-regression", records[0]["tags"])
        self.assertEqual(records[0]["saved_workflow_id"], "github_dual_code_audit")
        second = server._ensure_local_real_community_records()
        self.assertTrue(second[0]["duplicate"])

    def test_maintained_recording_is_verified_in_isolated_decoder_before_publish(self):
        storage = WorkflowStorage()
        workflow, subgraphs = storage.load("publish_me")
        recording = b"\x00\x00\x00\x18ftypmp42recording-evidence"
        recording_path = workflow.storage_dir / "recording.mp4"
        recording_path.write_bytes(recording)
        workflow.artifacts = {
            "recording": {
                "kind": "screen-recording",
                "path": "recording.mp4",
                "mime_type": "video/mp4",
                "bytes": len(recording),
                "sha256": hashlib.sha256(recording).hexdigest(),
            }
        }
        storage.save(workflow, subgraphs)
        server.MAINTAINED_COMMUNITY_WORKFLOWS = {
            "publish_me": {
                "author": "GPA Maintainers",
                "tags": ["benchmark-task"],
            }
        }
        report = {
            "schema": "gpa.recording-media-probe/v1",
            "status": "verified",
            "verified": True,
            "decoded_sample_count": 3,
        }

        with patch.object(server, "_run_isolated_media_probe", return_value=report) as probe:
            records = server._ensure_local_real_community_records()

        probe.assert_called_once_with(recording_path)
        self.assertTrue(records[0]["recording_verification"]["verified"])
        checks = {
            item["id"]: item
            for item in records[0]["reproduction_contract"]["checks"]
        }
        self.assertTrue(checks["recording_evidence"]["passed"])

    def test_assistantbench_seed_is_idempotent_and_preserves_recording_evidence(self):
        server._ensure_assistantbench_workflows()
        storage = WorkflowStorage()
        fubo, _ = storage.load("assistantbench_fubo_ipo_management")
        self.assertEqual(len(fubo.steps), 26)
        self.assertEqual(
            fubo.provenance["task_id"],
            "6f224e7730ed027cbac73aebb1aea7f954053082041b02b19f4ff126a0a8a208",
        )
        self.assertEqual(fubo.provenance["gold_answer"], "Gina DiGioia")
        self.assertIn("stockanalysis.com", fubo.provenance["gold_urls"][0])
        self.assertTrue(all("ir.fubo.tv" in url for url in fubo.provenance["reproduction_urls"]))
        self.assertIn("preserved verbatim", fubo.provenance["reproduction_source_policy"])
        fidelity, _ = storage.load("assistantbench_fidelity_emerging_markets")
        self.assertEqual(len(fidelity.steps), 58)
        self.assertEqual(
            fidelity.provenance["task_id"],
            "efc0f3a47e9ed2ecdbcc037c2093865fe6e39f4d413a5d1ccdc7357160a4606b",
        )
        self.assertEqual(
            fidelity.provenance["gold_answer"],
            "Fidelity® Emerging Markets Index Fund (FPADX)",
        )
        self.assertEqual(len(fidelity.provenance["gold_urls"]), 6)
        self.assertEqual(len(fidelity.provenance["reproduction_urls"]), 4)
        self.assertTrue(all("fundresearch.fidelity.com" in url for url in fidelity.provenance["reproduction_urls"]))
        dog, _ = storage.load("assistantbench_dog_genome_files")
        self.assertEqual(len(dog.steps), 83)
        self.assertEqual(
            dog.provenance["task_id"],
            "929b45f34805280d77c61d1e093e3d4e551d77ddb6ecd73552b12b1af286388d",
        )
        chicago, _ = storage.load("assistantbench_chicago_new_year_snow")
        self.assertEqual(len(chicago.steps), 95)
        self.assertEqual(
            chicago.provenance["task_id"],
            "e2dc3a6b10b762e8aba7fa4d4e70f757f6d04dcbc8b56c48fc53fd9928d31d07",
        )
        self.assertEqual(chicago.provenance["gold_answer"], "30")
        self.assertEqual(len(chicago.provenance["gold_urls"]), 10)
        self.assertEqual(chicago.provenance["calculation"]["formula"], "3 / 10 * 100")
        self.assertEqual(
            sum(step.action_type == "assert_not_text" for step in chicago.steps), 7
        )
        chicago_yaml_before = (chicago.storage_dir / "workflow.yaml").read_bytes()
        self.assertEqual(
            dog.provenance["gold_answer"],
            "ftp://ftp.broadinstitute.org/distribution/assemblies/mammals/dog/canFam3.1/",
        )
        self.assertEqual(dog.steps[-3].action_type, "assert_link")
        self.assertEqual(dog.steps[53].action_type, "assert_url")
        self.assertEqual(dog.steps[53].value, "pmc.ncbi.nlm.nih.gov")
        self.assertTrue(dog.steps[53].metadata["redirect_verified"])
        self.assertEqual(
            dog.provenance["resolved_source_urls"][
                "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC3953330/"
            ],
            "https://pmc.ncbi.nlm.nih.gov/articles/PMC3953330/",
        )
        self.assertIn(
            "pmc.ncbi.nlm.nih.gov",
            dog.understanding["required_environment"]["web_hosts"],
        )
        workflow, subgraphs = storage.load("assistantbench_apple_board")
        recording = workflow.storage_dir / "recording.mp4"
        recording.write_bytes(b"recording-evidence")
        workflow.artifacts = {
            "recording": {
                "kind": "screen-recording",
                "path": "recording.mp4",
                "mime_type": "video/mp4",
                "bytes": recording.stat().st_size,
                "sha256": "fixture",
            }
        }
        storage.save(workflow, subgraphs)

        server._ensure_assistantbench_workflows()
        reloaded, _ = storage.load("assistantbench_apple_board")

        self.assertEqual(reloaded.artifacts, workflow.artifacts)
        self.assertEqual(recording.read_bytes(), b"recording-evidence")
        self.assertEqual(
            (chicago.storage_dir / "workflow.yaml").read_bytes(),
            chicago_yaml_before,
        )

    def test_community_write_rejects_foreign_origin(self):
        handler = DummyHandler(
            {"workflow_id": "publish_me", "privacy_reviewed": True},
            origin="https://attacker.example",
        )

        server._publish_community_record(handler)

        self.assertEqual(handler.status, 403)
        self.assertEqual(server._community_repository().list_records(), [])

    def test_community_write_rejects_null_origin(self):
        handler = DummyHandler(
            {"workflow_id": "publish_me", "privacy_reviewed": True},
            origin="null",
        )

        server._publish_community_record(handler)

        self.assertEqual(handler.status, 403)
        self.assertEqual(server._community_repository().list_records(), [])


if __name__ == "__main__":
    unittest.main()
