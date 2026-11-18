"""Preview or repair saved Notion extraction without network access."""
import json

from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder

from opportunities.services.reprocessing import reprocess_saved_items


class Command(BaseCommand):
    help = "Reprocess saved Notion snapshots. Preview by default; --apply saves safe repairs."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Save the previewed repairs.")
        parser.add_argument("--json", action="store_true", dest="as_json", help="Print a JSON report.")

    def handle(self, *args, **options):
        report = reprocess_saved_items(apply=options["apply"])
        if options["as_json"]:
            self.stdout.write(json.dumps(report, cls=DjangoJSONEncoder, ensure_ascii=False, indent=2))
        else:
            self.stdout.write(
                f"{report['mode'].capitalize()}: {report['items_updated']} imported items repaired; "
                f"{report['drafts_updated']} drafts repaired; {report['drafts_created']} drafts created; "
                f"{report['preserved_fields']} edited fields preserved; "
                f"{report['protected']} published/archived entries protected; {report['errors']} errors."
            )
            if not options["apply"]:
                self.stdout.write("Preview only: no records were changed. Use --apply to save.")
            for detail in report["details"]:
                self.stdout.write(f"\nImported item {detail.get('id', detail.get('external_id', '?'))}: {detail.get('title', '')}")
                if "error" in detail:
                    self.stdout.write(f"  Error: {detail['error']}")
                    continue
                for group in ("changes", "draft_changes"):
                    for field, change in detail[group].items():
                        self.stdout.write(
                            f"  {group}: {field}: {change['before']!r} -> {change['after']!r}"
                        )
                if detail["preserved_fields"] or detail["preserved_draft_fields"]:
                    self.stdout.write("  Staff edits preserved; compare the saved source snapshot.")
                if detail["required_draft_fields_preserved"]:
                    self.stdout.write("  Required draft fields need manual correction; existing values preserved.")
        if report["errors"]:
            raise CommandError("Some snapshots could not be repaired. See the report; those items were unchanged.")
