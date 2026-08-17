import os
import unittest
from unittest.mock import patch

import click
from click.testing import CliRunner

from gpa.integration import cli


class CLITests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_help_and_version_present_product_identity(self):
        help_result = self.runner.invoke(cli.main, ["--help"])
        version_result = self.runner.invoke(cli.main, ["--version"])

        self.assertEqual(help_result.exit_code, 0, help_result.output)
        self.assertIn("local-first GUI workflow", help_result.output)
        self.assertEqual(version_result.exit_code, 0, version_result.output)
        self.assertIn("gpa, version 0.1.0", version_result.output)

    def test_list_empty_repository_is_successful(self):
        with patch.object(cli.wf_storage, "list_workflows", return_value=[]):
            result = self.runner.invoke(cli.main, ["list"])

        self.assertEqual(result.exit_code, 0, result.output)

    def test_resolve_id_supports_exact_name_and_unique_prefix(self):
        rows = [
            {"id": "alpha-123", "name": "Alpha"},
            {"id": "beta-456", "name": "Beta"},
        ]
        with patch.object(cli.wf_storage, "list_workflows", return_value=rows):
            self.assertEqual(cli._resolve_id("alpha-123"), "alpha-123")
            self.assertEqual(cli._resolve_id("Beta"), "beta-456")
            self.assertEqual(cli._resolve_id("alpha"), "alpha-123")
            self.assertIsNone(cli._resolve_id("missing"))

    def test_resolve_id_refuses_ambiguous_name_or_prefix(self):
        rows = [
            {"id": "alpha-123", "name": "Repeated"},
            {"id": "alpha-456", "name": "Repeated"},
        ]
        with patch.object(cli.wf_storage, "list_workflows", return_value=rows):
            with self.assertRaisesRegex(click.ClickException, "name is ambiguous"):
                cli._resolve_id("Repeated")
            with self.assertRaisesRegex(click.ClickException, "prefix is ambiguous"):
                cli._resolve_id("alpha")

    def test_run_fails_closed_before_loading_workflow(self):
        rows = [{"id": "alpha-123", "name": "Alpha"}]
        with (
            patch.dict(os.environ, {cli.DESKTOP_AUTOMATION_ENV: "0"}),
            patch.object(cli.wf_storage, "list_workflows", return_value=rows),
            patch.object(cli.wf_storage, "load") as load,
        ):
            result = self.runner.invoke(cli.main, ["run", "Alpha"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Desktop automation is disabled", result.output)
        load.assert_not_called()

    def test_run_rejects_invalid_runtime_configuration(self):
        with patch.dict(os.environ, {cli.DESKTOP_AUTOMATION_ENV: "perhaps"}):
            result = self.runner.invoke(cli.main, ["run", "Alpha"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn(cli.DESKTOP_AUTOMATION_ENV, result.output)

    def test_run_validates_numeric_options_before_execution(self):
        for arguments in (
            ["run", "Alpha", "--threshold", "1.1"],
            ["run", "Alpha", "--retries", "-1"],
            ["run", "Alpha", "--retries", "51"],
        ):
            with self.subTest(arguments=arguments):
                result = self.runner.invoke(cli.main, arguments)
                self.assertEqual(result.exit_code, 2, result.output)

    def test_run_rejects_empty_variable_key_before_loading(self):
        rows = [{"id": "alpha-123", "name": "Alpha"}]
        with (
            patch.dict(os.environ, {cli.DESKTOP_AUTOMATION_ENV: "1"}),
            patch.object(cli.wf_storage, "list_workflows", return_value=rows),
            patch.object(cli.wf_storage, "load") as load,
        ):
            result = self.runner.invoke(cli.main, ["run", "Alpha", "--var", "=value"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Invalid --var key", result.output)
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
