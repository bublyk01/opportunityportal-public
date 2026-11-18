from datetime import date

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from opportunities.models import ImportedItem, Opportunity
from opportunities.services.importing import import_candidates, promote_imported_item
from opportunities.services.slack_source import (
    CHANNEL_ID,
    CHANNEL_URL,
    SOURCE_KEY,
    SlackChannel,
    get_channel,
    get_channels,
    message_permalink,
    validate_channel_url,
    validate_message_identity,
)
from opportunities.services.source_identity import normalize_source_identity


MESSAGE_ID = "1794200400.123456"
SECOND_CHANNEL_ID = "C2222222222"
SECOND_CHANNEL_URL = f"https://workspace.slack.example.invalid/archives/{SECOND_CHANNEL_ID}"
SECOND_SOURCE_KEY = f"slack-example-workspace-{SECOND_CHANNEL_ID}"


def slack_candidate(**overrides):
    candidate = {
        "external_id": MESSAGE_ID,
        "source_url": message_permalink(MESSAGE_ID),
        "title": "Engineering internship",
        "organization": "Example employer",
        "type": Opportunity.Type.INTERNSHIP,
        "description": "An opportunity posted in the configured careers channel.",
        "location": "Example City",
        "deadline": date(2026, 11, 30),
        "raw_data": {"text": "Original Slack message", "warnings": ["Review message extraction."]},
    }
    candidate.update(overrides)
    return candidate


@override_settings(SLACK_CHANNEL_URLS=(CHANNEL_URL,))
class SlackSourceValidationTests(SimpleTestCase):
    def test_only_exact_configured_channel_url_is_accepted(self):
        self.assertEqual(validate_channel_url(CHANNEL_URL), CHANNEL_URL)
        for invalid in (
            CHANNEL_URL.replace("https:", "http:"),
            CHANNEL_URL.replace("workspace.slack.example.invalid", "anotherworkspace.slack.example.invalid"),
            CHANNEL_URL.replace(CHANNEL_ID, "C0000000000"),
            CHANNEL_URL.replace("slack.example.invalid", "slack.example.invalid.attacker.test"),
            CHANNEL_URL.replace("https://", "https://user:password@"),
            CHANNEL_URL.replace("slack.example.invalid", "slack.example.invalid:443"),
            CHANNEL_URL + "/",
            CHANNEL_URL + "?thread_ts=" + MESSAGE_ID,
            CHANNEL_URL + "?",
            CHANNEL_URL + "#",
            CHANNEL_URL + "\n",
            CHANNEL_URL.replace("archives", "%61rchives"),
            None,
        ):
            with self.subTest(url=invalid), self.assertRaises(ValueError):
                validate_channel_url(invalid)

    def test_exact_message_identity_round_trip(self):
        url = f"{CHANNEL_URL}/p1794200400123456"
        self.assertEqual(message_permalink(MESSAGE_ID), url)
        self.assertEqual(validate_message_identity(MESSAGE_ID, url), (MESSAGE_ID, url))
        self.assertEqual(
            normalize_source_identity("slack", SOURCE_KEY, MESSAGE_ID, url),
            (MESSAGE_ID, url),
        )

    def test_message_timestamp_must_not_be_rounded_or_coerced(self):
        for invalid in (
            1794200400.123456, None, "1794200400", "1794200400.12345",
            "1794200400.1234567", "1794200400123456", "1794200400.123456\n",
            "１７９４２００４００.１２３４５６", "1794200400.123456/../../",
        ):
            with self.subTest(message_id=invalid), self.assertRaises(ValueError):
                message_permalink(invalid)

    def test_message_url_must_match_exact_channel_timestamp_and_source_key(self):
        url = message_permalink(MESSAGE_ID)
        for invalid in (
            url.replace("123456", "654321"),
            url.replace(CHANNEL_ID, "C0000000000"),
            url + "?thread_ts=" + MESSAGE_ID,
            url + "#thread",
            url.replace("https:", "http:"),
        ):
            with self.subTest(url=invalid), self.assertRaises(ValueError):
                validate_message_identity(MESSAGE_ID, invalid)
        with self.assertRaises(ValueError):
            normalize_source_identity("slack", "arbitrary-channel", MESSAGE_ID, url)
        with self.assertRaises(ValueError):
            normalize_source_identity("manual", SOURCE_KEY, MESSAGE_ID, url)


@override_settings(SLACK_CHANNEL_URLS=(CHANNEL_URL, SECOND_CHANNEL_URL))
class SlackConfiguredChannelTests(SimpleTestCase):
    def test_configured_channels_resolve_by_canonical_identity_and_keep_order(self):
        channels = get_channels()
        self.assertEqual(len(channels), 2)
        self.assertEqual([channel.channel_id for channel in channels], [CHANNEL_ID, SECOND_CHANNEL_ID])
        self.assertEqual(get_channel(), channels[0])
        second = channels[1]
        self.assertEqual(second.url, SECOND_CHANNEL_URL)
        self.assertEqual(second.source_key, SECOND_SOURCE_KEY)
        for selector in (SECOND_CHANNEL_URL, SECOND_CHANNEL_ID, SECOND_SOURCE_KEY, second):
            with self.subTest(selector=selector):
                self.assertEqual(get_channel(selector), second)
        self.assertEqual(validate_channel_url(SECOND_CHANNEL_URL), SECOND_CHANNEL_URL)
        with self.assertRaises(AttributeError):
            second.channel_id = CHANNEL_ID

    def test_string_configuration_deduplicates_without_losing_order(self):
        with override_settings(SLACK_CHANNEL_URLS=f" {SECOND_CHANNEL_URL}, {CHANNEL_URL}, {SECOND_CHANNEL_URL} "):
            self.assertEqual([channel.url for channel in get_channels()], [SECOND_CHANNEL_URL, CHANNEL_URL])
            self.assertEqual(get_channel().channel_id, SECOND_CHANNEL_ID)
            self.assertEqual(message_permalink(MESSAGE_ID), SECOND_CHANNEL_URL + "/p1794200400123456")

    def test_each_message_permalink_has_its_own_channel_and_source_key(self):
        second_url = message_permalink(MESSAGE_ID, channel=SECOND_CHANNEL_ID)
        self.assertEqual(second_url, SECOND_CHANNEL_URL + "/p1794200400123456")
        self.assertEqual(validate_message_identity(MESSAGE_ID, second_url), (MESSAGE_ID, second_url))
        self.assertEqual(
            validate_message_identity(MESSAGE_ID, second_url, channel=SECOND_SOURCE_KEY, source_key=SECOND_SOURCE_KEY),
            (MESSAGE_ID, second_url),
        )
        self.assertEqual(
            normalize_source_identity("slack", SECOND_SOURCE_KEY, MESSAGE_ID, second_url),
            (MESSAGE_ID, second_url),
        )
        for options in ({"channel": CHANNEL_ID}, {"source_key": SOURCE_KEY}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                validate_message_identity(MESSAGE_ID, second_url, **options)
        with self.assertRaises(ValueError):
            normalize_source_identity("slack", SOURCE_KEY, MESSAGE_ID, second_url)
        with self.assertRaises(ValueError):
            normalize_source_identity("slack", None, MESSAGE_ID, second_url)

    def test_unconfigured_selectors_and_forged_channel_objects_are_rejected(self):
        for selector in (
            "C0000000000", "slack-example-workspace-C0000000000",
            "https://workspace.slack.example.invalid/archives/C0000000000",
            SECOND_CHANNEL_URL + "?thread_ts=" + MESSAGE_ID,
            SECOND_CHANNEL_URL.replace("workspace.slack.example.invalid", "otherworkspace.slack.example.invalid"),
            SlackChannel(SECOND_CHANNEL_ID, CHANNEL_URL, SECOND_SOURCE_KEY),
            SlackChannel(SECOND_CHANNEL_ID, SECOND_CHANNEL_URL, SOURCE_KEY),
            {},
        ):
            with self.subTest(selector=selector), self.assertRaises(ValueError):
                get_channel(selector)
        with self.assertRaises(ValueError):
            validate_channel_url(SECOND_CHANNEL_ID)

    def test_invalid_allowlist_never_enables_unapproved_workspace_or_malformed_channels(self):
        for configured in (
            (), None, 123,
            (CHANNEL_URL, SECOND_CHANNEL_URL.replace("workspace.slack.example.invalid", "otherworkspace.slack.example.invalid")),
            (SECOND_CHANNEL_URL + "/",),
            (SECOND_CHANNEL_URL + "?",),
            (SECOND_CHANNEL_URL.replace("slack.example.invalid", "slack.example.invalid:443"),),
            (SECOND_CHANNEL_URL.replace(SECOND_CHANNEL_ID, "C123"),),
            (SECOND_CHANNEL_URL.replace(SECOND_CHANNEL_ID, "G2222222222"),),
            (SECOND_CHANNEL_URL.replace(SECOND_CHANNEL_ID, "Caaaaaaaaaa"),),
            (SECOND_CHANNEL_URL.replace(SECOND_CHANNEL_ID, "C" + "A" * 32),),
        ):
            with self.subTest(configured=configured), override_settings(SLACK_CHANNEL_URLS=configured):
                with self.assertRaises(ValueError):
                    get_channels()


@override_settings(SLACK_CHANNEL_URLS=(CHANNEL_URL,))
class SlackPersistenceTests(TestCase):
    def import_slack(self, *candidates, **options):
        return import_candidates(
            candidates, source_key=SOURCE_KEY, source=Opportunity.Source.SLACK, **options,
        )

    def test_complete_message_creates_validated_slack_draft_with_review_evidence(self):
        report = self.import_slack(slack_candidate(status="published", source="manual"))
        self.assertEqual(report["created"], 1)
        self.assertEqual(report["errors"], 0)
        item = ImportedItem.objects.get()
        opportunity = item.opportunity
        self.assertEqual(item.source, Opportunity.Source.SLACK)
        self.assertEqual(opportunity.source, Opportunity.Source.SLACK)
        self.assertEqual(opportunity.status, Opportunity.Status.DRAFT)
        self.assertEqual(opportunity.source_url, message_permalink(MESSAGE_ID))
        self.assertEqual(opportunity.deadline, date(2026, 11, 30))
        self.assertTrue(item.review_required)
        self.assertIn("Review message extraction.", item.notes)
        self.assertEqual(item.source_payload["raw_data"]["text"], "Original Slack message")
        item.full_clean()
        opportunity.full_clean()

    def test_repeated_message_is_deduplicated_and_distinct_microseconds_are_distinct(self):
        self.import_slack(slack_candidate())
        self.assertEqual(self.import_slack(slack_candidate())["unchanged"], 1)
        next_id = "1794200400.123457"
        report = self.import_slack(slack_candidate(
            external_id=next_id, source_url=message_permalink(next_id),
        ))
        self.assertEqual(report["created"], 1)
        self.assertEqual(ImportedItem.objects.count(), 2)
        self.assertEqual(Opportunity.objects.count(), 2)

    def test_incomplete_message_can_be_corrected_and_promoted_once_with_slack_source(self):
        report = self.import_slack(slack_candidate(organization=""))
        self.assertEqual(report["staged"], 1)
        item = ImportedItem.objects.get()
        self.assertFalse(Opportunity.objects.exists())
        with self.assertRaises(ValidationError):
            promote_imported_item(item.pk)
        item.organization = "Reviewed employer"
        item.save()
        opportunity = promote_imported_item(item.pk)
        self.assertEqual(opportunity.source, Opportunity.Source.SLACK)
        self.assertEqual(opportunity.status, Opportunity.Status.DRAFT)
        self.assertEqual(opportunity.organization, "Reviewed employer")
        self.assertIsNone(promote_imported_item(item.pk))
        self.assertEqual(Opportunity.objects.count(), 1)

    def test_edited_source_preserves_staff_fields_and_all_linked_opportunity_statuses(self):
        self.import_slack(slack_candidate())
        item = ImportedItem.objects.get()
        item.organization = "Staff corrected employer"
        item.notes = "Staff notes"
        item.save()
        opportunity = item.opportunity
        for status in Opportunity.Status.values:
            with self.subTest(status=status):
                opportunity.status = status
                opportunity.title = "Staff title"
                opportunity.save()
                report = self.import_slack(slack_candidate(title=f"Edited Slack title {status}"))
                item.refresh_from_db()
                opportunity.refresh_from_db()
                self.assertEqual(report["changed"], 1)
                self.assertEqual(item.organization, "Staff corrected employer")
                self.assertEqual(item.notes, "Staff notes")
                self.assertEqual(item.source_payload["title"], f"Edited Slack title {status}")
                self.assertTrue(item.review_required)
                self.assertEqual(opportunity.title, "Staff title")
                self.assertEqual(opportunity.status, status)

    def test_unapproved_source_key_rejects_slack_without_writing(self):
        report = import_candidates(
            [slack_candidate()], source_key="slack-other-workspace", source=Opportunity.Source.SLACK,
        )
        self.assertEqual(report["errors"], 1)
        self.assertFalse(ImportedItem.objects.exists())
        self.assertFalse(Opportunity.objects.exists())

    def test_invalid_message_does_not_stop_valid_sibling(self):
        report = self.import_slack(
            slack_candidate(source_url=message_permalink(MESSAGE_ID).replace(CHANNEL_ID, "C0000000000")),
            slack_candidate(),
        )
        self.assertEqual(report["errors"], 1)
        self.assertEqual(report["created"], 1)
        self.assertEqual(ImportedItem.objects.count(), 1)

    def test_existing_import_source_and_url_identity_cannot_be_reassigned(self):
        self.import_slack(slack_candidate())
        item = ImportedItem.objects.get()
        original_seen = item.last_seen
        ImportedItem.objects.filter(pk=item.pk).update(source=Opportunity.Source.NOTION)
        report = self.import_slack(slack_candidate())
        self.assertEqual(report["errors"], 1)
        item.refresh_from_db()
        self.assertEqual(item.last_seen, original_seen)
        self.assertEqual(item.source, Opportunity.Source.NOTION)
        ImportedItem.objects.filter(pk=item.pk).update(
            source=Opportunity.Source.SLACK, source_url=CHANNEL_URL,
        )
        report = self.import_slack(slack_candidate())
        self.assertEqual(report["errors"], 1)
        item.refresh_from_db()
        self.assertEqual(item.source_url, CHANNEL_URL)

    def test_model_full_clean_dispatches_slack_identity_and_defaults_legacy_source_to_notion(self):
        self.assertEqual(ImportedItem().source, Opportunity.Source.NOTION)
        self.import_slack(slack_candidate())
        item = ImportedItem.objects.get()
        for field, value in (
            ("source_key", "unapproved"),
            ("source", Opportunity.Source.MANUAL),
            ("external_id", "123"),
            ("source_url", CHANNEL_URL),
        ):
            with self.subTest(field=field):
                item.refresh_from_db()
                setattr(item, field, value)
                with self.assertRaises(ValidationError):
                    item.full_clean()


@override_settings(SLACK_CHANNEL_URLS=(CHANNEL_URL, SECOND_CHANNEL_URL))
class SlackMultiChannelPersistenceTests(TestCase):
    def import_channel(self, channel, **overrides):
        selected = get_channel(channel)
        return import_candidates(
            [slack_candidate(source_url=message_permalink(MESSAGE_ID, channel=selected), **overrides)],
            source_key=selected.source_key, source=Opportunity.Source.SLACK,
        )

    def test_identical_timestamps_in_two_channels_remain_independent_and_repeat_safe(self):
        self.assertEqual(self.import_channel(CHANNEL_ID)["created"], 1)
        self.assertEqual(self.import_channel(SECOND_CHANNEL_ID)["created"], 1)
        self.assertEqual(self.import_channel(CHANNEL_ID)["unchanged"], 1)
        self.assertEqual(self.import_channel(SECOND_CHANNEL_ID)["unchanged"], 1)
        self.assertEqual(ImportedItem.objects.count(), 2)
        self.assertEqual(Opportunity.objects.count(), 2)
        first = ImportedItem.objects.get(source_key=SOURCE_KEY)
        second = ImportedItem.objects.get(source_key=SECOND_SOURCE_KEY)
        self.assertEqual(first.external_id, second.external_id)
        self.assertNotEqual(first.opportunity_id, second.opportunity_id)
        self.assertNotEqual(first.source_url, second.source_url)
        self.assertTrue(all(item.status == Opportunity.Status.DRAFT for item in Opportunity.objects.all()))
        first.full_clean()
        second.full_clean()

    def test_cross_channel_key_mismatch_cannot_update_an_existing_import(self):
        self.import_channel(CHANNEL_ID)
        original = ImportedItem.objects.get()
        original_snapshot = original.source_payload
        report = import_candidates(
            [slack_candidate(
                source_url=message_permalink(MESSAGE_ID, channel=SECOND_CHANNEL_ID), title="Cross-channel text",
            )],
            source_key=SOURCE_KEY, source=Opportunity.Source.SLACK,
        )
        self.assertEqual(report["errors"], 1)
        original.refresh_from_db()
        self.assertEqual(original.source_payload, original_snapshot)
        self.assertEqual(ImportedItem.objects.count(), 1)
        self.assertEqual(Opportunity.objects.count(), 1)

    def test_staged_second_channel_item_promotes_without_changing_channel_provenance(self):
        self.assertEqual(self.import_channel(SECOND_CHANNEL_ID, organization="")["staged"], 1)
        item = ImportedItem.objects.get()
        item.organization = "Reviewed employer"
        item.save()
        opportunity = promote_imported_item(item.pk)
        self.assertEqual(opportunity.source, Opportunity.Source.SLACK)
        self.assertEqual(opportunity.source_url, message_permalink(MESSAGE_ID, channel=SECOND_CHANNEL_ID))
        self.assertEqual(opportunity.status, Opportunity.Status.DRAFT)

    def test_removing_channel_from_allowlist_blocks_validation_and_promotion(self):
        self.import_channel(SECOND_CHANNEL_ID, organization="")
        item = ImportedItem.objects.get()
        item.organization = "Reviewed employer"
        item.save()
        with override_settings(SLACK_CHANNEL_URLS=(CHANNEL_URL,)):
            with self.assertRaises(ValidationError):
                item.full_clean()
            with self.assertRaises(ValidationError):
                promote_imported_item(item.pk)
        item.refresh_from_db()
        self.assertIsNone(item.opportunity_id)
        self.assertFalse(Opportunity.objects.exists())
