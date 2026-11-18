"""Preview or import the configured public Notion opportunity database."""
import json

from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder

from opportunities.services.importing import import_candidates
from opportunities.services.notion_public import (
    SOURCE_KEY,
    NotionImportError,
    scrape_public_notion,
)


class Command(BaseCommand):
    help = (
        "Preview public Notion listings. Add --import to save validated drafts "
        "and incomplete listings for admin review."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--import",
            action="store_true",
            dest="commit",
            help="Save the collected listings; the default is a preview only.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            help="Collect an explicit batch of 1-500 listings instead of all listings.",
        )
        parser.add_argument(
            "--browser",
            choices=("chrome", "msedge", "chromium"),
            default="chrome",
            dest="browser_channel",
            help="Browser to use (default: the installed Google Chrome).",
        )
        parser.add_argument(
            "--headed",
            action="store_true",
            help="Show the separate importer browser for normal site verification.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Print a JSON preview or import report to standard output.",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        browser_channel = options["browser_channel"]
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500
        ):
            raise CommandError("--limit must be an integer between 1 and 500.")
        if browser_channel not in ("chrome", "msedge", "chromium"):
            raise CommandError("--browser must be chrome, msedge, or chromium.")

        try:
            browser_options = {"limit": limit, "browser_channel": browser_channel}
            if options["headed"]:
                browser_options["headed"] = True
            if options["verbosity"] >= 2:
                browser_options["progress"] = self.stderr.write
            result = scrape_public_notion(**browser_options)
        except (NotionImportError, ValueError) as exc:
            raise CommandError(str(exc)) from exc

        payload = {
            "mode": "import" if options["commit"] else "preview",
            "source": SOURCE_KEY,
            "complete": result.complete,
            "discovered": result.discovered,
            "collected": len(result.items),
            "limit": limit,
            "warnings": result.warnings,
            "items": result.items,
        }

        if not result.complete:
            if not options["commit"]:
                self.write_report(payload, as_json=options["as_json"])
            raise CommandError(
                "Collection is incomplete. Nothing was imported. "
                "Review the warnings and retry."
            )

        if options["commit"]:
            payload["summary"] = import_candidates(result.items, source_key=SOURCE_KEY)

        self.write_report(payload, as_json=options["as_json"])
        if payload.get("summary", {}).get("errors", 0):
            raise CommandError(
                "Some listings could not be imported. See the import report for details."
            )

    def write_report(self, payload, *, as_json):
        if as_json:
            self.stdout.write(
                json.dumps(payload, cls=DjangoJSONEncoder, ensure_ascii=False, indent=2)
            )
            return

        label = "Import" if payload["mode"] == "import" else "Preview"
        state = "complete" if payload["complete"] else "INCOMPLETE"
        self.stdout.write(
            f"{label}: {payload['collected']} collected; "
            f"{payload['discovered']} discovered; {state}."
        )
        if payload["mode"] == "preview":
            self.stdout.write("Preview only: no database records were changed.")
        for warning in payload["warnings"]:
            self.stdout.write(f"Warning: {warning}")
        for index, item in enumerate(payload["items"], start=1):
            self.stdout.write(f"\n{index}. {item.get('title') or '[Missing title]'}")
            self.stdout.write(
                f"   Organization: {item.get('organization') or '[Needs review]'}"
            )
            self.stdout.write(f"   Type: {item.get('type') or '[Needs review]'}")
            self.stdout.write(f"   Employment type: {item.get('employment_type') or '[Not supplied]'}")
            self.stdout.write(f"   Work mode: {item.get('work_mode') or '[Not supplied]'}")
            self.stdout.write(f"   Location: {item.get('location') or '[Not supplied]'}")
            self.stdout.write(f"   Deadline: {item.get('deadline') or '[Not supplied]'}")
            self.stdout.write(f"   Source: {item.get('source_url', '')}")
        if "summary" in payload:
            summary = payload["summary"]
            self.stdout.write(
                "\nSaved: "
                f"{summary['created']} drafts created; "
                f"{summary['staged']} staged for review; "
                f"{summary['unchanged']} unchanged; "
                f"{summary['changed']} source changes flagged; "
                f"{summary['errors']} errors."
            )
            for message in summary.get("messages", []):
                self.stdout.write(f"  {message}")
