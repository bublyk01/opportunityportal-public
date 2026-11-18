from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from opportunities.models import ImportedItem, Opportunity
from opportunities.services.importing import import_candidates, promote_imported_item
from opportunities.services.notion_public import SOURCE_KEY as NOTION_SOURCE_KEY
from opportunities.services.slack_source import CHANNEL_URL, SOURCE_KEY as SLACK_SOURCE_KEY, message_permalink


def event_candidate(index=1, **overrides):
    external_id = f"{index:032x}"
    fields = {
        "external_id": external_id,
        "source_url": f"https://notion.example.invalid/{external_id}",
        "title": "Research seminar",
        "organization": "Example University",
        "type": Opportunity.Type.EVENT,
        "description": "An open research seminar for students.",
        "raw_data": {"text": "Original announcement"},
    }
    fields.update(overrides)
    return fields


@override_settings(SLACK_CHANNEL_URLS=(CHANNEL_URL,))
class EventFormatModelTests(TestCase):
    def opportunity(self, **overrides):
        fields = {"title": "Student opportunity", "organization": "University", "type": Opportunity.Type.EVENT}
        fields.update(overrides)
        return Opportunity(**fields)

    def imported_item(self, **overrides):
        fields = {
            "source_key": NOTION_SOURCE_KEY, "external_id": "1" * 32,
            "source_url": "https://notion.example.invalid/" + "1" * 32,
            "source_hash": "a" * 64, "source_payload": {"text": "Announcement"},
            "type": Opportunity.Type.EVENT,
        }
        fields.update(overrides)
        return ImportedItem(**fields)

    def test_all_supported_formats_round_trip_without_changing_broad_event_category(self):
        for subtype in Opportunity.EventType.values:
            with self.subTest(event_type=subtype):
                opportunity = self.opportunity(event_type=subtype)
                opportunity.full_clean()
                opportunity.save()
                opportunity.refresh_from_db()
                self.assertEqual(opportunity.type, Opportunity.Type.EVENT)
                self.assertEqual(opportunity.event_type, subtype)
                item = self.imported_item(event_type=subtype)
                item.full_clean()

    def test_blank_format_remains_optional_for_every_existing_category(self):
        for kind in Opportunity.Type.values:
            with self.subTest(kind=kind):
                opportunity = self.opportunity(type=kind)
                opportunity.full_clean()
                self.assertEqual(opportunity.event_type, "")
                item = self.imported_item(type=kind)
                item.full_clean()
                self.assertEqual(item.event_type, "")

    def test_jobs_internships_and_unclassified_imports_cannot_claim_an_event_format(self):
        for kind in (Opportunity.Type.JOB, Opportunity.Type.INTERNSHIP):
            for factory in (self.opportunity, self.imported_item):
                with self.subTest(kind=kind, model=factory.__name__):
                    with self.assertRaises(ValidationError) as error:
                        factory(type=kind, event_type=Opportunity.EventType.SEMINAR).full_clean()
                    self.assertIn("event_type", error.exception.message_dict)
        with self.assertRaises(ValidationError):
            self.imported_item(type="", event_type=Opportunity.EventType.SEMINAR).full_clean()

    def test_unknown_format_is_rejected_by_both_models(self):
        for factory in (self.opportunity, self.imported_item):
            with self.subTest(model=factory.__name__), self.assertRaises(ValidationError) as error:
                factory(event_type="invented").full_clean()
            self.assertIn("event_type", error.exception.message_dict)


@override_settings(SLACK_CHANNEL_URLS=(CHANNEL_URL,))
class EventFormatImportTests(TestCase):
    def test_missing_and_blank_event_formats_are_inferred_when_importing_drafts(self):
        report = import_candidates(
            [event_candidate(1), event_candidate(2, event_type=" ")], source_key=NOTION_SOURCE_KEY,
        )
        self.assertEqual(report["created"], 2)
        for item in ImportedItem.objects.select_related("opportunity"):
            self.assertEqual(item.event_type, Opportunity.EventType.SEMINAR)
            self.assertEqual(item.opportunity.event_type, Opportunity.EventType.SEMINAR)
            self.assertEqual(item.opportunity.status, Opportunity.Status.DRAFT)
            self.assertEqual(item.source_payload["event_type"], Opportunity.EventType.SEMINAR)

    def test_explicit_format_survives_inference_and_staged_promotion(self):
        report = import_candidates(
            [event_candidate(organization="", event_type=Opportunity.EventType.LECTURE)],
            source_key=NOTION_SOURCE_KEY,
        )
        self.assertEqual(report["staged"], 1)
        item = ImportedItem.objects.get()
        self.assertEqual(item.event_type, Opportunity.EventType.LECTURE)
        item.organization = "Staff supplied university"
        item.save()
        opportunity = promote_imported_item(item.pk)
        self.assertEqual(opportunity.event_type, Opportunity.EventType.LECTURE)
        self.assertEqual(opportunity.type, Opportunity.Type.EVENT)

    def test_job_mentioning_seminars_does_not_receive_an_inferred_event_format(self):
        report = import_candidates(
            [event_candidate(type=Opportunity.Type.JOB, title="Seminar coordinator")],
            source_key=NOTION_SOURCE_KEY,
        )
        self.assertEqual(report["created"], 1)
        self.assertEqual(Opportunity.objects.get().event_type, "")

    def test_inconsistent_or_unknown_source_formats_fail_validation_before_saving(self):
        for overrides in (
            {"type": Opportunity.Type.JOB, "event_type": Opportunity.EventType.SEMINAR},
            {"event_type": "unknown-format"},
        ):
            with self.subTest(overrides=overrides):
                report = import_candidates([event_candidate(**overrides)], source_key=NOTION_SOURCE_KEY)
                self.assertEqual(report["errors"], 1)
                self.assertFalse(ImportedItem.objects.exists())
                self.assertFalse(Opportunity.objects.exists())

    def test_changed_source_preserves_staff_event_formats_on_import_and_published_opportunity(self):
        import_candidates([event_candidate()], source_key=NOTION_SOURCE_KEY)
        item = ImportedItem.objects.get()
        item.event_type = Opportunity.EventType.LECTURE
        item.save()
        opportunity = item.opportunity
        opportunity.event_type = Opportunity.EventType.WORKSHOP
        opportunity.status = Opportunity.Status.PUBLISHED
        opportunity.save()
        report = import_candidates(
            [event_candidate(event_type=Opportunity.EventType.CONFERENCE)], source_key=NOTION_SOURCE_KEY,
        )
        self.assertEqual(report["changed"], 1)
        item.refresh_from_db()
        opportunity.refresh_from_db()
        self.assertEqual(item.event_type, Opportunity.EventType.LECTURE)
        self.assertEqual(item.source_payload["event_type"], Opportunity.EventType.CONFERENCE)
        self.assertEqual(opportunity.event_type, Opportunity.EventType.WORKSHOP)
        self.assertEqual(opportunity.status, Opportunity.Status.PUBLISHED)


@override_settings(SECURE_SSL_REDIRECT=False)
@override_settings(SLACK_CHANNEL_URLS=(CHANNEL_URL,))
class EventFormatAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model()
        cls.admin = users.objects.create_superuser(username="event-admin", password=None)
        cls.viewer = users.objects.create_user(username="event-viewer", is_staff=True)
        cls.viewer.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="opportunities",
            codename__in=("view_opportunity", "view_importeditem"),
        ))
        cls.items = {}
        fixtures = (
            ("Notion seminar", Opportunity.Type.EVENT, Opportunity.EventType.SEMINAR, "notion"),
            ("Notion lecture", Opportunity.Type.EVENT, Opportunity.EventType.LECTURE, "notion"),
            ("Slack seminar", Opportunity.Type.EVENT, Opportunity.EventType.SEMINAR, "slack"),
            ("Slack workshop", Opportunity.Type.EVENT, Opportunity.EventType.WORKSHOP, "slack"),
            ("Notion internship", Opportunity.Type.INTERNSHIP, "", "notion"),
            ("Slack job", Opportunity.Type.JOB, "", "slack"),
        )
        for index, (title, kind, subtype, source) in enumerate(fixtures, start=1):
            candidate = event_candidate(index, title=title, type=kind, event_type=subtype)
            source_key = NOTION_SOURCE_KEY
            if source == "slack":
                timestamp = f"1794200400.{index:06d}"
                candidate.update(external_id=timestamp, source_url=message_permalink(timestamp))
                source_key = SLACK_SOURCE_KEY
            report = import_candidates([candidate], source_key=source_key, source=source)
            if report["created"] != 1:
                raise AssertionError(report)
            cls.items[title] = ImportedItem.objects.get(source_key=source_key, external_id=candidate["external_id"])

    def list_titles(self, model, params):
        response = self.client.get(reverse(f"admin:opportunities_{model}_changelist"), params)
        self.assertEqual(response.status_code, 200)
        return set(response.context["cl"].queryset.values_list("title", flat=True))

    def test_hide_seminars_keeps_jobs_internships_and_other_event_formats_on_both_lists(self):
        self.client.force_login(self.admin)
        expected = {"Notion lecture", "Slack workshop", "Notion internship", "Slack job"}
        for model in ("opportunity", "importeditem"):
            with self.subTest(model=model):
                self.assertEqual(self.list_titles(model, {"hide_seminars": "1"}), expected)
                self.assertEqual(len(self.list_titles(model, {})), 6)

    def test_event_format_type_and_source_filters_combine(self):
        self.client.force_login(self.admin)
        for model in ("opportunity", "importeditem"):
            with self.subTest(model=model):
                self.assertEqual(self.list_titles(model, {
                    "type__exact": "event", "event_type__exact": "workshop",
                    "source__exact": "slack", "hide_seminars": "1",
                }), {"Slack workshop"})
                self.assertEqual(self.list_titles(model, {"type__exact": "internship"}), {"Notion internship"})
        self.assertEqual(self.list_titles("importeditem", {
            "source_key__exact": SLACK_SOURCE_KEY, "hide_seminars": "1",
        }), {"Slack workshop", "Slack job"})

    def test_staff_forms_show_optional_event_format_and_validate_category_consistency(self):
        self.client.force_login(self.admin)
        item = self.items["Notion internship"]
        for model, record in (("opportunity", item.opportunity), ("importeditem", item)):
            with self.subTest(model=model):
                response = self.client.get(reverse(f"admin:opportunities_{model}_change", args=[record.pk]))
                field = response.context["adminform"].form.fields["event_type"]
                self.assertEqual(field.label, "Event format")
                self.assertFalse(field.required)
        response = self.client.post(
            reverse("admin:opportunities_opportunity_change", args=[item.opportunity_id]),
            {
                "title": "Notion internship", "organization": "Example University", "type": "internship",
                "event_type": "seminar", "status": "draft", "source": "notion", "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("event_type", response.context["adminform"].form.errors)
        item.opportunity.refresh_from_db()
        self.assertEqual(item.opportunity.event_type, "")

    def test_view_permission_can_filter_but_cannot_edit_new_field(self):
        self.client.force_login(self.viewer)
        for model in ("opportunity", "importeditem"):
            with self.subTest(model=model):
                self.assertEqual(self.list_titles(model, {"event_type__exact": "seminar"}), {
                    "Notion seminar", "Slack seminar",
                })
        item = self.items["Notion seminar"]
        for model, pk in (("opportunity", item.opportunity_id), ("importeditem", item.pk)):
            with self.subTest(model=model):
                response = self.client.post(reverse(f"admin:opportunities_{model}_change", args=[pk]), {
                    "event_type": "lecture", "_save": "Save",
                })
                self.assertEqual(response.status_code, 403)
        item.refresh_from_db()
        item.opportunity.refresh_from_db()
        self.assertEqual(item.event_type, Opportunity.EventType.SEMINAR)
        self.assertEqual(item.opportunity.event_type, Opportunity.EventType.SEMINAR)
