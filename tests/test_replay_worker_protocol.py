import unittest

from gpa.replay.worker_protocol import (
    SCHEMA,
    DesktopReplayProtocol,
    DesktopWorkerProtocolError,
    validate_desktop_worker_result,
)


def event(name, **payload):
    return {"schema": SCHEMA, "event": name, **payload}


def valid_result(**updates):
    result = {
        "success": True,
        "error": "",
        "n_steps": 1,
        "n_failed": 0,
        "steps": [],
        "llm_metrics": [],
    }
    result.update(updates)
    return result


class ReplayWorkerProtocolTests(unittest.TestCase):
    def test_ignores_unknown_schema_and_unknown_events(self):
        protocol = DesktopReplayProtocol()

        self.assertIsNone(protocol.accept({"schema": "other", "event": "ready"}))
        self.assertIsNone(protocol.accept(event("diagnostic", value="ok")))

    def test_accepts_complete_event_lifecycle(self):
        protocol = DesktopReplayProtocol()

        ready = protocol.accept(event("ready", total_steps=1))
        step = protocol.accept(event("step_start", step={"number": 1, "action": "Open"}))
        decision = protocol.accept(event(
            "agent_decision", step_number=1, decision={"action_type": "open_url"}
        ))
        result = protocol.accept(event("result", result=valid_result()))

        self.assertEqual(ready[1]["total_steps"], 1)
        self.assertEqual(step[0], "step_start")
        self.assertEqual(decision[0], "agent_decision")
        self.assertTrue(result[1]["result"]["success"])

    def test_rejects_duplicate_ready_and_events_after_result(self):
        protocol = DesktopReplayProtocol()
        protocol.accept(event("ready", total_steps=1))
        with self.assertRaisesRegex(DesktopWorkerProtocolError, "duplicate ready"):
            protocol.accept(event("ready", total_steps=1))

        protocol = DesktopReplayProtocol()
        protocol.accept(event("ready", total_steps=1))
        protocol.accept(event("result", result=valid_result()))
        with self.assertRaisesRegex(DesktopWorkerProtocolError, "out of order"):
            protocol.accept(event("step_start", step={"number": 1}))

    def test_rejects_invalid_ready_step_and_decision_payloads(self):
        for invalid in (True, -1, "1"):
            with self.subTest(total_steps=invalid), self.assertRaisesRegex(
                DesktopWorkerProtocolError, "total_steps"
            ):
                DesktopReplayProtocol().accept(event("ready", total_steps=invalid))

        protocol = DesktopReplayProtocol()
        protocol.accept(event("ready", total_steps=1))
        with self.assertRaisesRegex(DesktopWorkerProtocolError, "step_start payload"):
            protocol.accept(event("step_start", step=[]))
        with self.assertRaisesRegex(DesktopWorkerProtocolError, "agent_decision payload"):
            protocol.accept(event("agent_decision", decision=[]))

    def test_rejects_regressing_steps_wrong_decision_step_and_post_result_crash(self):
        protocol = DesktopReplayProtocol()
        protocol.accept(event("ready", total_steps=2))
        protocol.accept(event("step_start", step={"number": 1}))
        with self.assertRaisesRegex(DesktopWorkerProtocolError, "wrong step"):
            protocol.accept(event("agent_decision", step_number=2, decision={}))
        with self.assertRaisesRegex(DesktopWorkerProtocolError, "out-of-order step"):
            protocol.accept(event("step_start", step={"number": 1}))

        protocol = DesktopReplayProtocol()
        protocol.accept(event("ready", total_steps=0))
        protocol.accept(event("result", result=valid_result(n_steps=0)))
        with self.assertRaisesRegex(DesktopWorkerProtocolError, "crashed after"):
            protocol.accept(event("crash", error="late crash"))

    def test_rejects_inconsistent_result_contract(self):
        invalid_results = (
            valid_result(success="yes"),
            valid_result(n_steps=True),
            valid_result(n_steps=1, n_failed=2),
            valid_result(steps={}),
            valid_result(llm_metrics={}),
        )
        for invalid in invalid_results:
            with self.subTest(invalid=invalid), self.assertRaises(DesktopWorkerProtocolError):
                validate_desktop_worker_result(invalid)


if __name__ == "__main__":
    unittest.main()
