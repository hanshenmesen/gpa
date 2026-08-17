import unittest

import gpa.recording.builder as builder
from gpa.recording.recorder import RecordedEvent, Recording


class BuilderMergeTests(unittest.TestCase):
    def test_build_splits_navigation_clicks_and_different_hotkeys(self):
        recording = Recording(events=[
            RecordedEvent(event_type="click", x=10, y=20, active_app="Google Chrome"),
            RecordedEvent(event_type="click", x=30, y=40, active_app="Google Chrome"),
            RecordedEvent(event_type="hotkey", value="cmd+a", active_app="Google Chrome"),
            RecordedEvent(event_type="hotkey", value="cmd+c", active_app="Google Chrome"),
        ])
        old_call = builder._call_llm
        try:
            builder._call_llm = lambda *args, **kwargs: {
                "workflow_name": "atomic_actions",
                "workflow_title": "Atomic Actions",
                "description": "Keep executable actions atomic.",
                "variables": [],
                "steps": [
                    {
                        "event_indices": [1, 2],
                        "action_type": "click",
                        "description": "Navigate through two folders",
                    },
                    {
                        "event_indices": [3, 4],
                        "action_type": "hotkey",
                        "description": "Select and copy",
                        "value": "cmd+a, cmd+c",
                    },
                ],
                "discarded_events": [],
            }
            result = builder.build_workflow(recording, workflow_id="atomic_actions")
        finally:
            builder._call_llm = old_call

        self.assertEqual([step.action_type for step in result.workflow.steps], [
            "click", "click", "hotkey", "hotkey",
        ])
        self.assertEqual([step.value for step in result.workflow.steps], ["", "", "cmd+a", "cmd+c"])

    def test_build_restores_omitted_accessibility_actions(self):
        recording = Recording(events=[
            RecordedEvent(
                event_type="hotkey",
                value="cmd+f",
                active_app="Google Chrome",
                metadata={
                    "input_source": "accessibility_automation",
                    "target_hint": "browser find",
                    "target_url": "https://example.test/source.py",
                },
            ),
            RecordedEvent(
                event_type="type",
                value="needle",
                active_app="Google Chrome",
                metadata={"input_source": "accessibility_automation", "target_hint": "find field"},
            ),
            RecordedEvent(
                event_type="hotkey",
                value="enter",
                active_app="Google Chrome",
                metadata={"input_source": "accessibility_automation"},
            ),
        ])
        old_call = builder._call_llm
        try:
            builder._call_llm = lambda *args, **kwargs: {
                "workflow_name": "preserve_accessibility",
                "workflow_title": "Preserve Accessibility",
                "description": "Preserve reported actions.",
                "variables": [],
                "steps": [{
                    "event_indices": [2],
                    "action_type": "type",
                    "description": "Type search term",
                    "value": "needle",
                }],
                "discarded_events": [
                    {"event_index": 1, "reason": "low-level shortcut"},
                    {"event_index": 3, "reason": "low-level shortcut"},
                ],
            }
            result = builder.build_workflow(recording, workflow_id="preserve_accessibility")
        finally:
            builder._call_llm = old_call

        self.assertEqual([step.action_type for step in result.workflow.steps], ["hotkey", "type", "hotkey"])
        self.assertEqual([step.value for step in result.workflow.steps], ["cmd+f", "needle", "enter"])
        self.assertEqual(result.workflow.steps[0].metadata["target_hint"], "browser find")
        self.assertEqual(
            result.workflow.steps[0].metadata["input_source"],
            "accessibility_automation",
        )
        self.assertEqual(
            result.workflow.steps[0].metadata["target_url"],
            "https://example.test/source.py",
        )
        self.assertLessEqual(result.workflow.steps[0].pause_duration, 0.4)

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

    def test_build_splits_model_merged_text_for_distinct_fields(self):
        recording = Recording(events=[
            RecordedEvent(
                event_type="type",
                value="Ada",
                active_app="Google Chrome",
                metadata={"target_hint": "first name"},
            ),
            RecordedEvent(
                event_type="type",
                value="Lovelace",
                active_app="Google Chrome",
                metadata={"target_hint": "last name"},
            ),
        ])
        old_call = builder._call_llm
        try:
            builder._call_llm = lambda *args, **kwargs: {
                "workflow_name": "fill_name",
                "workflow_title": "Fill Name",
                "description": "Fill two name fields.",
                "variables": [],
                "steps": [{
                    "event_indices": [1, 2],
                    "action_type": "type",
                    "description": "Type the full name",
                    "value": "Ada Lovelace",
                    "variables": [],
                }],
                "discarded_events": [],
            }
            result = builder.build_workflow(recording, workflow_id="fill_name")
        finally:
            builder._call_llm = old_call

        self.assertEqual([step.value for step in result.workflow.steps], ["Ada", "Lovelace"])
        self.assertEqual(
            [step.metadata["target_hint"] for step in result.workflow.steps],
            ["first name", "last name"],
        )

    def test_fallback_does_not_merge_text_for_distinct_fields(self):
        events = [
            RecordedEvent(
                event_type="type", value="Ada", active_app="Safari",
                metadata={"target_hint": "first name"},
            ),
            RecordedEvent(
                event_type="type", value="Lovelace", active_app="Safari",
                metadata={"target_hint": "last name"},
            ),
        ]

        steps = builder._local_merge_steps(events)

        self.assertEqual(len(steps), 2)
        self.assertEqual([step["value"] for step in steps], ["Ada", "Lovelace"])

    def test_build_drops_llm_type_step_without_recorded_type_evidence(self):
        recording = Recording(events=[
            RecordedEvent(event_type="click", x=100, y=100, active_app="Google Chrome"),
            RecordedEvent(event_type="hotkey", value="cmd+a", active_app="Google Chrome"),
        ])
        old_call = builder._call_llm
        try:
            builder._call_llm = lambda *args, **kwargs: {
                "task_description": "Type a customer name.",
                "workflow_name": "type_customer",
                "workflow_title": "Type Customer",
                "description": "Types a customer name.",
                "variables": [{"name": "customer", "default_value": "ACME"}],
                "steps": [
                    {"event_indices": [1], "action_type": "click", "description": "Focus field"},
                    {
                        "event_indices": [2],
                        "action_type": "type",
                        "description": "Type customer",
                        "value": "{{customer}}",
                    },
                ],
                "discarded_events": [],
            }
            result = builder.build_workflow(recording, workflow_id="no_type_evidence")
        finally:
            builder._call_llm = old_call

        self.assertEqual([step.action_type for step in result.workflow.steps], ["click"])

    def test_build_accepts_captured_paste_as_type_evidence(self):
        recording = Recording(events=[
            RecordedEvent(
                event_type="hotkey",
                value="cmd+v",
                clipboard_after="ACME Shanghai",
                metadata={"clipboard_operation": "paste"},
                active_app="Google Chrome",
            ),
        ])
        old_call = builder._call_llm
        try:
            builder._call_llm = lambda *args, **kwargs: {
                "task_description": "Type a customer name.",
                "workflow_name": "paste_customer",
                "workflow_title": "Paste Customer",
                "description": "Pastes a customer name.",
                "variables": [{"name": "customer", "default_value": "ACME Shanghai"}],
                "steps": [
                    {
                        "event_indices": [1],
                        "action_type": "type",
                        "description": "Type customer",
                        "value": "{{customer}}",
                    },
                ],
                "discarded_events": [],
            }
            result = builder.build_workflow(recording, workflow_id="paste_type_evidence")
        finally:
            builder._call_llm = old_call

        self.assertEqual(len(result.workflow.steps), 1)
        self.assertEqual(result.workflow.steps[0].action_type, "type")
        self.assertEqual(result.workflow.steps[0].value, "{{customer}}")

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

    def test_fallback_does_not_merge_repeated_non_idempotent_hotkeys(self):
        events = [
            RecordedEvent(event_type="hotkey", value="enter", active_app="TextEdit"),
            RecordedEvent(
                event_type="hotkey", value="enter", active_app="TextEdit", pause_before=0.1,
            ),
            RecordedEvent(event_type="hotkey", value="cmd+v", active_app="TextEdit"),
            RecordedEvent(
                event_type="hotkey", value="cmd+v", active_app="TextEdit", pause_before=0.1,
            ),
        ]

        steps = builder._local_merge_steps(events)

        self.assertEqual([step["event_indices"] for step in steps], [[1], [2], [3], [4]])

    def test_event_indices_are_restored_to_recording_order(self):
        self.assertEqual(
            builder._event_indices({"event_indices": [3, 1, 2]}, 1, 3),
            [1, 2, 3],
        )

    def test_fallback_keeps_final_text_when_recorder_reports_progressive_corrections(self):
        events = [
            RecordedEvent(event_type="type", value="hel", active_app="TextEdit"),
            RecordedEvent(event_type="type", value="hello", active_app="TextEdit"),
            RecordedEvent(event_type="type", value="hello", active_app="TextEdit"),
        ]

        steps = builder._local_merge_steps(events)

        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["value"], "hello")

    def test_fallback_preserves_deletion_and_retyping_corrections(self):
        events = [
            RecordedEvent(
                event_type="type", value="hello", active_app="TextEdit",
                metadata={"target_hint": "document"},
            ),
            RecordedEvent(
                event_type="type", value="hell", active_app="TextEdit",
                metadata={"target_hint": "document"},
            ),
            RecordedEvent(
                event_type="type", value="help", active_app="TextEdit",
                metadata={"target_hint": "document"},
            ),
        ]

        steps = builder._local_merge_steps(events)

        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["value"], "help")

    def test_merged_step_records_intent_normalization_reason(self):
        recording = Recording(events=[
            RecordedEvent(
                event_type="type", value="hel", active_app="TextEdit",
                metadata={"target_hint": "document"},
            ),
            RecordedEvent(
                event_type="type", value="hello", active_app="TextEdit",
                metadata={"target_hint": "document"},
            ),
        ])
        old_call = builder._call_llm
        try:
            builder._call_llm = lambda *args, **kwargs: {
                "workflow_name": "typed_correction",
                "workflow_title": "Typed correction",
                "description": "Type final text.",
                "variables": [],
                "steps": [{
                    "event_indices": [1, 2],
                    "action_type": "type",
                    "description": "Type hello",
                    "value": "hello",
                }],
                "discarded_events": [],
            }
            result = builder.build_workflow(recording, workflow_id="typed_correction")
        finally:
            builder._call_llm = old_call

        normalization = result.workflow.steps[0].metadata["intent_normalization"]
        self.assertEqual(normalization["strategy"], "typed_correction_or_continuation")
        self.assertEqual(normalization["source_event_count"], 2)

    def test_build_records_analysis_and_removes_passive_duplicate_hotkey(self):
        recording = Recording(events=[
            RecordedEvent(event_type="hotkey", value="cmd+s", active_app="TextEdit"),
            RecordedEvent(event_type="hotkey", value="cmd+s", active_app="TextEdit", pause_before=0.1),
        ])
        old_call = builder._call_llm
        try:
            builder._call_llm = lambda *args, **kwargs: {
                "workflow_name": "save_once",
                "workflow_title": "Save Once",
                "description": "Save the document.",
                "variables": [],
                "steps": [
                    {"event_indices": [1], "action_type": "hotkey", "description": "Save", "value": "cmd+s"},
                    {"event_indices": [2], "action_type": "hotkey", "description": "Save", "value": "cmd+s"},
                ],
                "discarded_events": [],
            }
            result = builder.build_workflow(recording, workflow_id="save_once")
        finally:
            builder._call_llm = old_call

        self.assertEqual(len(result.workflow.steps), 1)
        self.assertEqual(result.workflow.steps[0].metadata["recorded_event_indices"], [1, 2])
        self.assertEqual(
            result.workflow.steps[0].metadata["intent_normalization"],
            {"strategy": "duplicate_hotkey", "source_event_count": 2},
        )
        analysis = result.workflow.provenance["recording_analysis"]
        self.assertEqual(analysis["source_event_count"], 2)
        self.assertEqual(analysis["represented_event_count"], 2)
        self.assertEqual(analysis["retained_step_count"], 1)
        self.assertEqual(analysis["merged_event_count"], 1)
        self.assertEqual(analysis["discarded_or_noise_event_count"], 0)
        self.assertEqual(analysis["step_reduction_count"], 1)
        self.assertEqual(analysis["deterministic_duplicate_count"], 1)

    def test_fallback_merges_scroll_burst_but_not_different_apps(self):
        events = [
            RecordedEvent(event_type="scroll", scroll_dy=-1, active_app="Safari"),
            RecordedEvent(event_type="scroll", scroll_dy=-2, active_app="Safari", pause_before=0.2),
            RecordedEvent(event_type="scroll", scroll_dy=-3, active_app="Preview", pause_before=0.2),
        ]

        steps = builder._local_merge_steps(events)

        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["event_indices"], [1, 2])
        self.assertEqual(steps[0]["action_type"], "scroll")
        self.assertEqual(steps[1]["event_indices"], [3])

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
        self.assertNotIn("recorded_clipboard_before", result.workflow.steps[1].metadata)
        self.assertTrue(result.workflow.steps[1].metadata["recorded_clipboard_changed"])

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
