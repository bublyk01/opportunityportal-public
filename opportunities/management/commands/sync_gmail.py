"""Preview/import opportunities from the configured Gmail Inbox and date window."""
import json

from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder

from opportunities.services.gmail_browser import GmailImportError, login_gmail, scrape_gmail
from opportunities.services.gmail_source import get_account
from opportunities.services.importing import import_candidates


SUMMARY_FIELDS = ("created", "staged", "unchanged", "changed", "errors")


class Command(BaseCommand):
    help = "Sign in, preview, or import opportunities from the configured Gmail Inbox (last 1-30 days)."

    def add_arguments(self, parser):
        parser.add_argument("--import", action="store_true", dest="commit", help="Save validated drafts and incomplete items; default is preview only.")
        parser.add_argument("--login", action="store_true", help="Open the separate browser for interactive sign-in only.")
        parser.add_argument("--headed", action="store_true", help="Show the importer browser during collection.")
        parser.add_argument("--days", type=int, default=30, help="Inbox lookback, 1-30 days (default 30).")
        parser.add_argument("--limit", type=int, help="Collect an explicit batch of 1-1000 messages instead of the entire requested window.")
        parser.add_argument("--browser", choices=("chrome", "msedge", "chromium"), default="chrome", dest="browser_channel")
        parser.add_argument("--json", action="store_true", dest="as_json", help="Print JSON; individual items can include private mail content.")
        parser.add_argument("--summary-only", action="store_true", help="Omit individual mail content, links, warnings, and validation details from output.")

    def handle(self, *args, **options):
        days, limit = options["days"], options["limit"]
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 30:
            raise CommandError("--days must be an integer between 1 and 30.")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000):
            raise CommandError("--limit must be an integer between 1 and 1000.")
        if options["browser_channel"] not in ("chrome", "msedge", "chromium"):
            raise CommandError("--browser must be chrome, msedge, or chromium.")
        if options["login"] and options["commit"]:
            raise CommandError("--login and --import cannot be combined. Sign in first, then scan.")
        try:
            account = get_account()
            if options["login"]:
                if not options["as_json"] and not options["summary_only"]:
                    self.stdout.write(f"Sign in as {account.email} in the separate Gmail browser.")
                    self.stdout.flush()
                login_gmail(browser_channel=options["browser_channel"])
                self.stdout.write(
                    json.dumps({"mode": "login", "source": account.source_key, "authenticated": True})
                    if options["as_json"] else "Sign-in complete. The local Gmail importer profile is ready."
                )
                return
            browser_options = {
                "days": days, "limit": limit, "browser_channel": options["browser_channel"],
                "headed": options["headed"],
            }
            if options["verbosity"] >= 2 and not options["summary_only"]:
                browser_options["progress"] = self.stderr.write
            result = scrape_gmail(**browser_options)
        except (GmailImportError, ValueError) as exc:
            if options["summary_only"]:
                raise CommandError("Gmail scan failed. Run sync_gmail --headed locally to inspect the problem.") from exc
            raise CommandError(str(exc)) from exc

        payload = {
            "mode": "import" if options["commit"] else "preview",
            "source": account.source_key,
            "scope": "inbox", "days": days, "limit": limit,
            "coverage": "requested_batch" if limit is not None else "inbox_window",
            "complete": result.complete,
            "full_window": result.complete and limit is None,
            "discovered": result.discovered, "skipped": result.skipped,
            "collected": len(result.items),
            "window_start": result.window_start, "window_end": result.window_end,
            "warnings": result.warnings, "items": result.items,
        }
        if not result.complete:
            self.write_report(payload, as_json=options["as_json"], summary_only=options["summary_only"])
            raise CommandError("Collection is incomplete. No Gmail messages were imported.")
        if options["commit"]:
            payload["summary"] = import_candidates(result.items, source_key=account.source_key, source="gmail")
        self.write_report(payload, as_json=options["as_json"], summary_only=options["summary_only"])
        if payload.get("summary", {}).get("errors"):
            raise CommandError("Some Gmail messages could not be imported. Review the import counts or run a local JSON report.")

    def write_report(self, payload, *, as_json, summary_only):
        if summary_only:
            payload = {
                key: value for key, value in payload.items()
                if key not in ("items", "warnings", "summary")
            } | {
                "warning_count": len(payload["warnings"]),
                **({"summary": {key: payload["summary"].get(key, 0) for key in SUMMARY_FIELDS}}
                   if "summary" in payload else {}),
            }
        if as_json:
            self.stdout.write(json.dumps(payload, cls=DjangoJSONEncoder, ensure_ascii=False, indent=2))
            return
        coverage = "requested batch" if payload["limit"] is not None else "requested Inbox window"
        self.stdout.write(
            f"Gmail {payload['mode']}: {payload['discovered']} messages read; "
            f"{payload['collected']} opportunity candidates; {payload['skipped']} messages not classified as opportunities; "
            f"{coverage} " + ("complete." if payload["complete"] else "INCOMPLETE.")
        )
        if payload["mode"] == "preview":
            self.stdout.write("Preview only: no database records were changed.")
            if not summary_only:
                for item in payload["items"]:
                    self.stdout.write(
                        f"- {item.get('title') or '[Needs title]'} | "
                        f"{item.get('organization') or '[Needs organization]'} | "
                        f"{item.get('type') or '[Needs type]'}"
                    )
        warning_count = payload.get("warning_count", len(payload.get("warnings", [])))
        if warning_count:
            self.stdout.write(f"Extraction warnings: {warning_count}.")
        if "summary" in payload:
            summary = payload["summary"]
            self.stdout.write(
                f"Saved: {summary['created']} drafts; {summary['staged']} staged; "
                f"{summary['unchanged']} unchanged; {summary['changed']} source changes flagged; "
                f"{summary['errors']} errors."
            )
