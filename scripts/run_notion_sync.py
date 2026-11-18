"""Windowless scheduled entry point with a local import log."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import os
from pathlib import Path
import sys
import traceback
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main():
    sys.path.insert(0, str(PROJECT_ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    log_directory = PROJECT_ROOT / ".logs"
    log_directory.mkdir(exist_ok=True)
    with (log_directory / "notion-sync.log").open("a", encoding="utf-8") as log:
        with redirect_stdout(log), redirect_stderr(log):
            print(f"\n[{datetime.now(ZoneInfo('UTC')).isoformat(timespec='seconds')}] Starting import", flush=True)
            try:
                import django
                from django.core.management import call_command
                from django.core.management.base import CommandError

                django.setup()
                try:
                    call_command("sync_notion", commit=True, verbosity=2, stdout=log, stderr=log)
                except CommandError as exc:
                    print(f"Import failed: {exc}", flush=True)
                    return 1
            except Exception:
                traceback.print_exc(file=log)
                return 1
            print("Import finished successfully.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
