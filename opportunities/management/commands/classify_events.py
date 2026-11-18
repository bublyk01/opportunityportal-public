"""Preview or fill missing event formats on already saved opportunities/imports."""
import json

from django.core.management.base import BaseCommand, CommandError

from opportunities.services.event_backfill import classify_saved_events


class Command(BaseCommand):
    help = "Classify missing event formats in saved records. Default: preview, without source access."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Fill missing formats; existing selections are preserved.")
        parser.add_argument("--json", action="store_true", dest="as_json")

    def handle(self, *args, **options):
        report = classify_saved_events(apply=options["apply"])
        if options["as_json"]:
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            label = "Updated" if options["apply"] else "Would update"
            self.stdout.write(
                f"{label} {report['opportunities_updated']} opportunities and "
                f"{report['imports_updated']} imported items. "
                f"{report['unclassified']} events need manual format selection."
            )
            if not options["apply"]:
                self.stdout.write("Preview only: nothing was changed. Add --apply to fill blank formats.")
            for detail in report["details"]:
                self.stdout.write(f"- {detail['model']} {detail['id']}: {detail['event_type']} — {detail['title']}")
            for error in report["errors"]:
                self.stderr.write(error)
        if report["errors"]:
            raise CommandError("Some event formats could not be classified; see the report.")
