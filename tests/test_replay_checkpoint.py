import tempfile
import unittest
from pathlib import Path

from gpa.replay.checkpoint import create_checkpoint, decide_checkpoint, write_checkpoint


class ReplayCheckpointTests(unittest.TestCase):
    def test_checkpoint_supports_approve_edit_and_reject_contract(self):
        base = create_checkpoint(
            run_id="run-1",
            workflow_id="workflow-1",
            intervention={"step": 3, "kind": "choose_target"},
            completed_steps=[1, 2],
            gate_decision_id="gate-1",
        )
        approved = decide_checkpoint(base, decision="approve")
        self.assertEqual(approved["status"], "resumable")
        edited = decide_checkpoint(
            base,
            decision="edit",
            patch={"target_contract": {"name": "Submit"}},
        )
        self.assertEqual(edited["decision"]["kind"], "edit")
        rejected = decide_checkpoint(base, decision="reject", feedback="Wrong account")
        self.assertEqual(rejected["status"], "rejected")

    def test_checkpoint_write_is_atomic_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.json"
            checkpoint = create_checkpoint(
                run_id="run-2",
                workflow_id="workflow-2",
                intervention={"step": 1},
                completed_steps=[],
            )
            write_checkpoint(path, checkpoint)
            self.assertIn('"checkpoint_id"', path.read_text())

    def test_edit_requires_patch(self):
        checkpoint = create_checkpoint(
            run_id="run", workflow_id="flow", intervention={"step": 1}, completed_steps=[]
        )
        with self.assertRaisesRegex(ValueError, "versioned patch"):
            decide_checkpoint(checkpoint, decision="edit")


if __name__ == "__main__":
    unittest.main()
