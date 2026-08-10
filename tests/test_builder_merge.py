import unittest

import gpa.recording.builder as builder
from gpa.recording.recorder import RecordedEvent, Recording


class BuilderMergeTests(unittest.TestCase):
    def test_llm_event_indices_merge_type_step_and_variable_value(self):
        recording = Recording(events=[
            RecordedEvent(event_type="type", value="hel", active_app="TextEdit", pause_before=0.1),
            RecordedEvent(event_type="type", value="hello@example.com", active_app="TextEdit", pause_before=0.2),
            RecordedEvent(event_type="hotkey", value="enter", active_app="TextEdit", pause_before=0.3),
        ])
        old_call = builder._call_llm
        try:
            builder._call_llm = lambda *args, **kwargs: {
                "task_description": "Type an email address and submit.",
                "workflow_name": "type_email",
                "workflow_title": "Type Email",
                "description": "Types an email address and submits it.",
                "variables": [
                    {
                        "name": "email_address",
                        "default_value": "hello@example.com",
                        "description": "Email address to type",
                    }
                ],
                "steps": [
                    {
                        "event_indices": [1, 2],
                        "action_type": "type",
                        "description": "Type email address",
                        "value": "{{email_address}}",
                        "variables": ["email_address"],
                    },
                    {
                        "event_indices": [3],
                        "action_type": "hotkey",
                        "description": "Submit the field",
                        "value": "enter",
                        "variables": [],
                    },
                ],
                "discarded_events": [],
            }

            result = builder.build_workflow(recording, workflow_id="merge_demo")
        finally:
            builder._call_llm = old_call

        self.assertEqual(len(result.workflow.steps), 2)
        self.assertEqual(result.workflow.steps[0].action_type, "type")
        self.assertEqual(result.workflow.steps[0].value, "{{email_address}}")
        self.assertEqual(result.workflow.steps[1].value, "enter")

    def test_fallback_merges_adjacent_type_and_duplicate_hotkeys(self):
        events = [
            RecordedEvent(event_type="type", value="hello", active_app="TextEdit"),
            RecordedEvent(event_type="type", value=" world", active_app="TextEdit"),
            RecordedEvent(event_type="hotkey", value="cmd+s", active_app="TextEdit", pause_before=0.2),
            RecordedEvent(event_type="hotkey", value="cmd+s", active_app="TextEdit", pause_before=0.3),
        ]

        steps = builder._local_merge_steps(events)

        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["event_indices"], [1, 2])
        self.assertEqual(steps[0]["value"], "hello world")
        self.assertEqual(steps[1]["event_indices"], [3, 4])
        self.assertEqual(steps[1]["value"], "cmd+s")

    def test_build_preserves_drag_and_copy_clipboard_metadata(self):
        copied = "Only the selected paragraph"
        recording = Recording(events=[
            RecordedEvent(
                event_type="drag",
                start_x=10,
                start_y=20,
                end_x=110,
                end_y=80,
                x=110,
                y=80,
                duration_seconds=0.4,
                active_app="Google Chrome",
            ),
            RecordedEvent(
                event_type="hotkey",
                value="cmd+c",
                clipboard_before="old",
                clipboard_after=copied,
                active_app="Google Chrome",
            ),
        ])
        old_call = builder._call_llm
        try:
            builder._call_llm = lambda *args, **kwargs: {
                "task_description": "Copy selected paragraph.",
                "workflow_name": "copy_selection",
                "workflow_title": "Copy Selection",
                "description": "Copies selected text.",
                "variables": [],
                "steps": [
                    {
                        "event_indices": [1],
                        "action_type": "drag",
                        "description": "Select the paragraph",
                        "value": "",
                        "variables": [],
                    },
                    {
                        "event_indices": [2],
                        "action_type": "hotkey",
                        "description": "Copy selected paragraph",
                        "value": "cmd+c",
                        "variables": [],
                    },
                ],
                "discarded_events": [],
            }

            result = builder.build_workflow(recording, workflow_id="copy_selection")
        finally:
            builder._call_llm = old_call

        self.assertEqual(result.workflow.steps[0].action_type, "drag")
        self.assertEqual(result.workflow.steps[0].metadata["drag_start"], [10, 20])
        self.assertEqual(result.workflow.steps[0].metadata["drag_end"], [110, 80])
        self.assertEqual(result.workflow.steps[1].metadata["recorded_clipboard_text"], copied)

    def test_scroll_metadata_preserves_recorded_delta(self):
        event = RecordedEvent(
            event_type="scroll",
            x=300,
            y=400,
            scroll_dx=1,
            scroll_dy=-5,
            active_app="Google Chrome",
        )

        metadata = builder._merged_step_metadata([event], "scroll")

        self.assertEqual(metadata["scroll_dx"], 1)
        self.assertEqual(metadata["scroll_dy"], -5)

    def test_scroll_metadata_sums_merged_recorded_deltas(self):
        events = [
            RecordedEvent(event_type="scroll", scroll_dx=0, scroll_dy=-1),
            RecordedEvent(event_type="scroll", scroll_dx=1, scroll_dy=-2),
            RecordedEvent(event_type="scroll", scroll_dx=0, scroll_dy=-3),
        ]

        metadata = builder._merged_step_metadata(events, "scroll")

        self.assertEqual(metadata["scroll_dx"], 1)
        self.assertEqual(metadata["scroll_dy"], -6)

    def test_build_prunes_codex_browser_focus_noise(self):
        recording = Recording(events=[
            RecordedEvent(event_type="click", x=300, y=60, active_app="Google Chrome"),
            RecordedEvent(event_type="type", value="acm technews", active_app="Google Chrome"),
            RecordedEvent(event_type="hotkey", value="enter", active_app="Google Chrome"),
            RecordedEvent(event_type="click", x=140, y=900, active_app="Codex"),
            RecordedEvent(event_type="hotkey", value="cmd+a", active_app="Google Chrome"),
        ])
        old_call = builder._call_llm
        try:
            builder._call_llm = lambda *args, **kwargs: {
                "task_description": "Open ACM TechNews and copy the page content.",
                "workflow_name": "copy_acm",
                "workflow_title": "Copy ACM TechNews",
                "description": "Open ACM TechNews and copy page content.",
                "variables": [],
                "steps": [
                    {
                        "event_indices": [1],
                        "action_type": "click",
                        "description": "Click the Chrome address bar",
                        "value": "",
                    },
                    {
                        "event_indices": [2],
                        "action_type": "type",
                        "description": "Type ACM TechNews",
                        "value": "acm technews",
                    },
                    {
                        "event_indices": [3],
                        "action_type": "hotkey",
                        "description": "Submit navigation",
                        "value": "enter",
                    },
                    {
                        "event_indices": [4],
                        "action_type": "click",
                        "description": "Click the loaded ACM TechNews page content to focus it",
                        "value": "",
                    },
                    {
                        "event_indices": [5],
                        "action_type": "hotkey",
                        "description": "Select all page content",
                        "value": "cmd+a",
                    },
                ],
                "discarded_events": [],
            }

            result = builder.build_workflow(recording, workflow_id="copy_acm")
        finally:
            builder._call_llm = old_call

        self.assertEqual([step.step_number for step in result.workflow.steps], [1, 2, 3, 4])
        self.assertNotIn("Codex", [step.active_app_name for step in result.workflow.steps])
        self.assertEqual(result.workflow.steps[-1].value, "cmd+a")


if __name__ == "__main__":
    unittest.main()
