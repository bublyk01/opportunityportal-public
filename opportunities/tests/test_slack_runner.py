import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase

from scripts import run_slack_sync


class SlackScheduledRunnerTests(SimpleTestCase):
    def run_worker(self, *, error=None):
        with TemporaryDirectory() as directory:
            project_root = Path(directory)
            with (
                patch.object(run_slack_sync, "PROJECT_ROOT", project_root),
                patch.object(run_slack_sync.sys, "path", list(run_slack_sync.sys.path)),
                patch.dict(os.environ, {"DJANGO_SETTINGS_MODULE": "config.settings"}),
                patch("django.setup"),
                patch("django.core.management.call_command", side_effect=error) as command,
            ):
                result = run_slack_sync.main()
            log = (project_root / ".logs" / "slack-sync.log").read_text(encoding="utf-8")
            return result, log, command

    def test_success_runs_headless_import_and_records_completion(self):
        result, log, command = self.run_worker()

        self.assertEqual(result, 0)
        self.assertEqual(command.call_args.args, ("sync_slack",))
        self.assertTrue(command.call_args.kwargs["commit"])
        self.assertFalse(command.call_args.kwargs["headed"])
        self.assertEqual(command.call_args.kwargs["verbosity"], 2)
        self.assertIs(command.call_args.kwargs["stdout"], command.call_args.kwargs["stderr"])
        self.assertIn("Starting Slack import", log)
        self.assertIn("Slack import finished successfully.", log)

    def test_sign_in_failure_returns_nonzero_and_does_not_log_success(self):
        result, log, _ = self.run_worker(error=CommandError("Run sync_slack --login."))

        self.assertEqual(result, 1)
        self.assertIn("Slack import failed: Run sync_slack --login.", log)
        self.assertNotIn("finished successfully", log)

    def test_unexpected_failure_returns_nonzero_with_local_diagnostic(self):
        result, log, _ = self.run_worker(error=RuntimeError("Unexpected import failure"))

        self.assertEqual(result, 1)
        self.assertIn("RuntimeError: Unexpected import failure", log)
        self.assertNotIn("finished successfully", log)
