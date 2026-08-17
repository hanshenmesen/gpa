import unittest

from gpa.replay.request import list_field, mapping_field, parse_replay_start_request


class ReplayStartRequestTests(unittest.TestCase):
    def test_shared_json_field_helpers_preserve_empty_values_and_reject_wrong_types(self):
        self.assertEqual(mapping_field({}, field="environment"), {})
        self.assertEqual(mapping_field(None, field="environment"), {})
        self.assertEqual(list_field([], field="tags"), [])
        self.assertEqual(list_field(None, field="tags"), [])
        with self.assertRaisesRegex(TypeError, "environment must be an object"):
            mapping_field([], field="environment")
        with self.assertRaisesRegex(TypeError, "tags must be a list"):
            list_field("", field="tags")

    def parse(self, body):
        return parse_replay_start_request(body, max_retries=50)

    def test_defaults_and_variable_normalization(self):
        request = self.parse({"variables": {"count": 3, "enabled": True}})

        self.assertEqual(request.execution_mode, "auto")
        self.assertEqual(request.variables, {"count": "3", "enabled": "True"})
        self.assertEqual(request.client_environment, {})
        self.assertEqual(request.threshold, 0.5)
        self.assertEqual(request.retries, 5)
        self.assertEqual(request.countdown_seconds, 3)
        self.assertEqual(request.max_runtime_seconds, 300)

    def test_clamps_bounded_runtime_values(self):
        request = self.parse({"countdown_seconds": 90, "max_runtime_seconds": 1})

        self.assertEqual(request.countdown_seconds, 30)
        self.assertEqual(request.max_runtime_seconds, 10)

    def test_preserves_explicit_empty_objects(self):
        request = self.parse({"variables": {}, "client_environment": {}})

        self.assertEqual(request.variables, {})
        self.assertEqual(request.client_environment, {})

    def test_rejects_falsey_non_object_payloads(self):
        for field in ("variables", "client_environment"):
            for invalid in ([], "", 0, False):
                with self.subTest(field=field, invalid=invalid):
                    with self.assertRaisesRegex(TypeError, f"{field} must be an object"):
                        self.parse({field: invalid})

    def test_rejects_boolean_non_finite_and_fractional_numbers(self):
        cases = (
            ({"threshold": True}, TypeError, "threshold must be a number"),
            ({"threshold": "nan"}, ValueError, "threshold must be finite"),
            ({"threshold": "inf"}, ValueError, "threshold must be finite"),
            ({"retries": False}, TypeError, "retries must be a number"),
            ({"retries": 1.5}, ValueError, "retries must be an integer"),
            ({"countdown_seconds": 2.2}, ValueError, "countdown_seconds must be an integer"),
        )
        for body, error_type, message in cases:
            with self.subTest(body=body), self.assertRaisesRegex(error_type, message):
                self.parse(body)

    def test_rejects_out_of_range_values_and_modes(self):
        cases = (
            ({"threshold": -0.1}, "threshold must be between"),
            ({"threshold": 1.1}, "threshold must be between"),
            ({"retries": -1}, "retries must be between"),
            ({"retries": 51}, "retries must be between"),
            ({"execution_mode": "native"}, "execution_mode must be"),
        )
        for body, message in cases:
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, message):
                self.parse(body)


if __name__ == "__main__":
    unittest.main()
