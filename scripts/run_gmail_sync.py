"""Run the scheduled Gmail import without writing private mail content to logs."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from io import StringIO
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SUMMARY_FIELDS = ("created", "staged", "unchanged", "changed", "errors")


def _aggregate_report(output):
    payload = json.loads(output)
    if not isinstance(payload, dict) or payload.get("mode") != "import":
        raise ValueError("The worker requires an import report.")
    counts = {key: payload[key] for key in ("discovered", "skipped", "collected")}
    summary = payload.get("summary", {})
    if not isinstance(summary, dict):
        raise ValueError("Expected import counts.")
    if payload.get("complete") is True and any(key not in summary for key in SUMMARY_FIELDS):
        raise ValueError("A completed import must include all outcome counts.")
    counts.update({key: summary.get(key, 0) for key in SUMMARY_FIELDS})
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts.values()):
        raise ValueError("Expected nonnegative counts.")
    if not isinstance(payload.get("complete"), bool):
        raise ValueError("Expected collection status.")
    return payload["complete"], counts


def main():
    sys.path.insert(0, str(PROJECT_ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    log_directory = PROJECT_ROOT / ".logs"
    log_directory.mkdir(exist_ok=True)
    with (log_directory / "gmail-sync.log").open("a", encoding="utf-8") as log:
        print(f"\n[{datetime.now(ZoneInfo('UTC')).isoformat(timespec='seconds')}] Starting Gmail import", file=log, flush=True)
        stdout, stderr = StringIO(), StringIO()
        failed = False
        failure_kind = ""
        # Playwright needs an OS stderr handle when launching its driver under
        # pythonw. Discard native diagnostics rather than inheriting Scheduler's
        # console handles or writing browser URLs into the import log.
        with open(os.devnull, "w", encoding="utf-8") as driver_stderr, \
                redirect_stdout(stdout), redirect_stderr(driver_stderr):
            try:
                import django
                from django.core.management import call_command

                django.setup()
                call_command(
                    "sync_gmail", commit=True, headed=False, summary_only=True,
                    as_json=True, verbosity=1, stdout=stdout, stderr=stderr,
                )
            except Exception as exc:
                # Error strings and tracebacks can contain subjects or body text.
                failed = True
                failure_kind = type(exc).__name__
                if exc.__cause__ is not None:
                    failure_kind += "/" + type(exc.__cause__).__name__
                    code = getattr(exc.__cause__, "code", "")
                    if code in {
                        "collection", "browser", "account", "search-loading", "body-loading",
                        "inbox-label", "grouped", "message-identity", "message-date", "clipped", "time-budget",
                    }:
                        failure_kind += f" ({code})"
        try:
            complete, counts = _aggregate_report(stdout.getvalue())
        except (ValueError, KeyError, TypeError):
            complete, counts = False, None
            failed = True
        if counts is not None:
            print(
                f"Read {counts['discovered']} messages; {counts['collected']} candidates; "
                f"{counts['skipped']} skipped; collection {'complete' if complete else 'incomplete'}. "
                f"Saved: {counts['created']} drafts; {counts['staged']} staged; "
                f"{counts['unchanged']} unchanged; {counts['changed']} changed; {counts['errors']} errors.",
                file=log, flush=True,
            )
            failed = failed or not complete or bool(counts["errors"])
        if failed:
            if failure_kind:
                print(f"Failure class: {failure_kind}", file=log, flush=True)
            print("Gmail import failed. Run sync_gmail --headed locally for details.", file=log, flush=True)
            return 1
        print("Gmail import finished successfully.", file=log, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
