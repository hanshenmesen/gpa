import hashlib
import json
import tempfile
import threading
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

import gpa.community.repository as repository_module
import gpa.storage.workflow as workflow_module
from gpa.community.package import export_workflow_package, inspect_workflow_package
from gpa.community.repository import CommunityRepository
from gpa.storage.workflow import Workflow, WorkflowStep, WorkflowStorage


class CommunityRepositoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workflows_dir = self.root / "workflows"
        self.workflows_dir.mkdir()
        self._old_workflows_dir = workflow_module.WORKFLOWS_DIR
        workflow_module.WORKFLOWS_DIR = self.workflows_dir
        self.storage = WorkflowStorage()
        self.storage.save(
            Workflow(
                workflow_id="shareable",
                workflow_name="shareable_record",
                workflow_title="Shareable Record",
                description="A browser workflow for community sharing.",
                task_description="Open an article and scroll it.",
                category="browser",
                provenance={
                    "kind": "public-benchmark",
                    "benchmark": "AssistantBench",
                    "task_id": "task-1",
                    "source_url": "https://assistantbench.github.io/",
                },
                steps=[
                    WorkflowStep(
                        step_number=1,
                        action="Scroll the article",
                        id="scroll-step",
                        action_type="scroll",
                        active_app_name="Google Chrome",
                        metadata={"scroll_dx": 0, "scroll_dy": -4},
                    )
                ],
            ),
            {},
        )
        self.package_path = export_workflow_package(
            "shareable",
            self.root / "packages",
            storage=self.storage,
        )
        self.repository = CommunityRepository(self.root / "community")

    def tearDown(self):
        workflow_module.WORKFLOWS_DIR = self._old_workflows_dir
        self._tmp.cleanup()

    def publish(self):
        return self.repository.publish_package(
            self.package_path,
            author="Alice",
            tags=["Browser", "Demo", "browser"],
            license_id="CC-BY-4.0",
            privacy_reviewed=True,
        )

    def test_publish_list_detail_download_import_and_feedback(self):
        record = self.publish()

        self.assertTrue(record["record_id"].startswith("rec_"))
        self.assertEqual(record["workflow_title"], "Shareable Record")
        self.assertEqual(record["tags"], ["browser", "demo"])
        self.assertFalse(record["duplicate"])
        self.assertEqual(record["provenance"]["task_id"], "task-1")

        duplicate = self.publish()
        self.assertEqual(duplicate["record_id"], record["record_id"])
        self.assertTrue(duplicate["duplicate"])

        listed = self.repository.list_records(query="article")
        self.assertEqual([item["record_id"] for item in listed], [record["record_id"]])
        self.assertEqual(listed[0]["provenance"]["benchmark"], "AssistantBench")

        self.repository.register_download(record["record_id"])
        imported = self.repository.import_record(
            record["record_id"],
            workflow_id="community_copy",
            storage=self.storage,
        )
        self.assertEqual(imported.workflow_id, "community_copy")
        loaded, _ = self.storage.load("community_copy")
        self.assertEqual(loaded.workflow_title, "Shareable Record")
        self.assertEqual(
            loaded.provenance["community_import"]["record_id"],
            record["record_id"],
        )
        self.assertEqual(
            loaded.provenance["community_import"]["package_sha256"],
            record["package_sha256"],
        )
        imported_metadata = json.loads(
            (self.workflows_dir / "community_copy" / "metadata.json").read_text()
        )
        self.assertEqual(
            imported_metadata["config_file"],
            str(self.workflows_dir / "community_copy" / "workflow.yaml"),
        )

        self.repository.add_feedback(
            record["record_id"],
            success=True,
            note="Worked on my Mac.",
            environment={"os": "macOS", "app": "Google Chrome"},
        )
        self.repository.add_feedback(
            record["record_id"],
            success=False,
            failed_step=1,
            note="Button moved.",
        )

        detail = self.repository.get_record(record["record_id"], include_feedback=True)
        self.assertEqual(detail["stats"]["downloads"], 1)
        self.assertEqual(detail["stats"]["imports"], 1)
        self.assertEqual(detail["stats"]["feedback_count"], 2)
        self.assertEqual(detail["stats"]["success_count"], 1)
        self.assertEqual(detail["stats"]["failure_count"], 1)
        self.assertEqual(detail["stats"]["success_rate"], 0.5)
        self.assertEqual(len(detail["recent_feedback"]), 2)

    def test_publish_requires_explicit_privacy_review(self):
        with self.assertRaisesRegex(ValueError, "privacy"):
            self.repository.publish_package(
                self.package_path,
                author="Alice",
                tags=[],
                license_id="CC-BY-4.0",
                privacy_reviewed=False,
            )

    def test_publish_path_must_stay_inside_repository_workspace(self):
        with tempfile.TemporaryDirectory() as outside:
            outside_package = Path(outside) / "outside.gpa-record.zip"
            outside_package.write_bytes(self.package_path.read_bytes())
            with self.assertRaisesRegex(ValueError, "community data workspace"):
                self.repository.publish_package(
                    outside_package,
                    author="Alice",
                    tags=[],
                    license_id="CC-BY-4.0",
                    privacy_reviewed=True,
                )

    def test_publisher_declaration_requires_every_commitment(self):
        with self.assertRaisesRegex(ValueError, "Publisher declaration"):
            self.repository.publish_package(
                self.package_path,
                author="Alice",
                tags=[],
                license_id="CC-BY-4.0",
                privacy_reviewed=True,
                publisher_declaration={
                    "owns_rights": True,
                    "no_secrets": True,
                    "safe_content": False,
                    "public_consent": True,
                },
            )

    def test_high_severity_report_restricts_distribution_until_operator_approval(self):
        record = self.publish()
        report = self.repository.submit_report(
            record["record_id"],
            category="privacy_or_credentials",
            details="The recording appears to contain a reusable credential.",
            report_id="rpt_privacy_fixture",
        )

        self.assertEqual(report["record_status"], "restricted")
        self.assertEqual(self.repository.list_records(), [])
        with self.assertRaisesRegex(PermissionError, "restricted"):
            self.repository.import_record(
                record["record_id"],
                workflow_id="restricted_copy",
                storage=self.storage,
            )
        overview = self.repository.moderation_overview()
        self.assertEqual(overview["records"]["by_status"]["restricted"], 1)
        self.assertEqual(overview["reports"]["open"], 1)

        approved = self.repository.moderate_record(
            record["record_id"],
            action="approve",
            reason="Credential report reviewed and confirmed as a false positive.",
        )
        self.assertEqual(approved["moderation"]["status"], "published")
        self.assertEqual(approved["moderation"]["trust_tier"], "operator_reviewed")
        self.assertEqual(len(self.repository.list_records()), 1)
        self.assertEqual(self.repository.moderation_overview()["reports"]["open"], 0)

    def test_report_and_appeal_retries_are_idempotent(self):
        record = self.publish()
        first = self.repository.submit_report(
            record["record_id"],
            category="malware",
            details="Suspicious command execution.",
            report_id="rpt_idempotent_fixture",
        )
        retry = self.repository.submit_report(
            record["record_id"],
            category="malware",
            details="Suspicious command execution.",
            report_id="rpt_idempotent_fixture",
        )
        self.assertFalse(first["duplicate"])
        self.assertTrue(retry["duplicate"])

        appeal = self.repository.submit_appeal(
            record["record_id"],
            explanation="The command is a documented, read-only package inspection step.",
            appeal_id="apl_idempotent_fixture",
        )
        appeal_retry = self.repository.submit_appeal(
            record["record_id"],
            explanation="The command is a documented, read-only package inspection step.",
            appeal_id="apl_idempotent_fixture",
        )
        self.assertFalse(appeal["duplicate"])
        self.assertTrue(appeal_retry["duplicate"])

    def test_duplicate_publish_can_upgrade_isolated_media_verification(self):
        record = self.publish()
        verification = {
            "schema": "gpa.recording-media-probe/v1",
            "status": "verified",
            "verified": True,
            "decoded_sample_count": 3,
        }

        upgraded = self.repository.publish_package(
            self.package_path,
            author="Alice",
            tags=["browser"],
            license_id="CC-BY-4.0",
            privacy_reviewed=True,
            recording_verification=verification,
        )

        self.assertTrue(upgraded["duplicate"])
        self.assertEqual(upgraded["record_id"], record["record_id"])
        self.assertTrue(upgraded["recording_verification"]["verified"])
        persisted = self.repository.get_record(record["record_id"])
        self.assertEqual(persisted["recording_verification"]["decoded_sample_count"], 3)
        listed = self.repository.list_records()
        self.assertTrue(listed[0]["recording_verification"]["verified"])

    def test_declared_recording_without_probe_is_not_treated_as_verified(self):
        workflow, subgraphs = self.storage.load("shareable")
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
                "capture_scope": "monitor",
                "privacy_review": {
                    "status": "failed",
                    "other_apps_visible": True,
                    "scope_confirmed": "monitor",
                },
            }
        }
        self.storage.save(workflow, subgraphs)
        package = export_workflow_package(
            "shareable",
            self.root / "recording-packages",
            storage=self.storage,
        )

        record = self.repository.publish_package(
            package,
            author="Alice",
            tags=["browser"],
            license_id="CC-BY-4.0",
            privacy_reviewed=True,
        )

        checks = {
            item["id"]: item
            for item in record["reproduction_contract"]["checks"]
        }
        self.assertFalse(checks["recording_evidence"]["passed"])
        self.assertFalse(record["reproduction_contract"]["publishable_as_verified"])
        with self.assertRaisesRegex(PermissionError, "Privacy-quarantined"):
            self.repository.import_record(
                record["record_id"],
                workflow_id="must_not_import",
                storage=self.storage,
            )

    def test_store_save_is_idempotent_and_saved_state_tracks_local_workflow(self):
        record = self.publish()

        first = self.repository.import_record(
            record["record_id"],
            workflow_id="saved_from_store",
            storage=self.storage,
        )
        second = self.repository.import_record(
            record["record_id"],
            workflow_id="ignored_on_repeat",
            storage=self.storage,
        )

        self.assertFalse(first.already_saved)
        self.assertTrue(second.already_saved)
        self.assertEqual(second.workflow_id, "saved_from_store")
        self.assertEqual(
            self.repository.list_records()[0]["saved_workflow_id"],
            "saved_from_store",
        )
        detail = self.repository.get_record(record["record_id"])
        self.assertEqual(detail["saved_workflow_id"], "saved_from_store")
        self.assertEqual(detail["stats"]["imports"], 1)

        self.storage.delete("saved_from_store")
        self.repository.forget_saved_workflow("saved_from_store")
        self.assertEqual(
            self.repository.get_record(record["record_id"])["saved_workflow_id"],
            "",
        )

    def test_import_rolls_back_when_provenance_persistence_fails(self):
        record = self.publish()

        with patch.object(self.storage, "save", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.repository.import_record(
                    record["record_id"],
                    workflow_id="orphan_candidate",
                    storage=self.storage,
                )

        self.assertFalse((self.workflows_dir / "orphan_candidate").exists())
        self.assertEqual(
            self.repository.get_record(record["record_id"])["saved_workflow_id"],
            "",
        )
        self.assertEqual(
            self.repository.get_record(record["record_id"])["stats"]["imports"],
            0,
        )

    def test_package_inspection_rejects_extra_or_oversized_archives(self):
        with self.assertRaisesRegex(ValueError, "maximum package size"):
            inspect_workflow_package(self.package_path, max_package_bytes=10)
        with self.assertRaisesRegex(ValueError, "maximum uncompressed size"):
            inspect_workflow_package(self.package_path, max_uncompressed_bytes=10)

        bad_path = self.root / "extra.gpa-record.zip"
        with zipfile.ZipFile(self.package_path) as source, zipfile.ZipFile(bad_path, "w") as target:
            for info in source.infolist():
                target.writestr(info, source.read(info.filename))
            target.writestr("undeclared.txt", "not allowed")

        with self.assertRaisesRegex(ValueError, "undeclared"):
            inspect_workflow_package(bad_path)

        duplicate_path = self.root / "duplicate.gpa-record.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.package_path) as source, zipfile.ZipFile(duplicate_path, "w") as target:
                for info in source.infolist():
                    target.writestr(info, source.read(info.filename))
                target.writestr(source.infolist()[0].filename, source.read(source.infolist()[0].filename))

        with self.assertRaisesRegex(ValueError, "Duplicate package member"):
            inspect_workflow_package(duplicate_path)

    def test_invalid_record_id_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "record_id"):
            self.repository.get_record("../escape")

    def test_concurrent_publish_is_idempotent_and_feedback_is_not_lost(self):
        barrier = threading.Barrier(4)
        record_ids = []
        errors = []

        def publish_worker():
            try:
                barrier.wait(timeout=2)
                record_ids.append(self.publish()["record_id"])
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=publish_worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

        self.assertFalse(errors)
        self.assertEqual(len(set(record_ids)), 1)
        self.assertEqual(len(self.repository.list_records()), 1)

        record_id = record_ids[0]
        feedback_errors = []

        def feedback_worker(index):
            try:
                self.repository.add_feedback(
                    record_id,
                    success=True,
                    note=f"run {index}",
                    feedback_id=f"fb_concurrent_{index:02d}",
                )
            except Exception as exc:
                feedback_errors.append(exc)

        threads = [threading.Thread(target=feedback_worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

        self.assertFalse(feedback_errors)
        detail = self.repository.get_record(record_id, include_feedback=True)
        self.assertEqual(detail["stats"]["feedback_count"], 8)
        self.assertEqual(detail["stats"]["success_count"], 8)

    def test_duplicate_feedback_retry_reconciles_stats_after_partial_write(self):
        record = self.publish()
        real_atomic_json = repository_module._atomic_json
        failed_once = False

        def fail_first_feedback_stats(path, payload):
            nonlocal failed_once
            if (
                not failed_once
                and path.name == repository_module.RECORD_FILENAME
                and payload.get("stats", {}).get("feedback_count") == 1
            ):
                failed_once = True
                raise OSError("simulated record write failure")
            return real_atomic_json(path, payload)

        with patch.object(repository_module, "_atomic_json", side_effect=fail_first_feedback_stats):
            with self.assertRaisesRegex(OSError, "simulated"):
                self.repository.add_feedback(
                    record["record_id"],
                    success=True,
                    feedback_id="fb_reconcile_retry",
                )

        retry = self.repository.add_feedback(
            record["record_id"],
            success=True,
            feedback_id="fb_reconcile_retry",
        )
        detail = self.repository.get_record(record["record_id"], include_feedback=True)
        self.assertTrue(retry["duplicate"])
        self.assertEqual(detail["stats"]["feedback_count"], 1)
        self.assertEqual(detail["stats"]["success_count"], 1)


if __name__ == "__main__":
    unittest.main()
