import hashlib

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from opportunities.models import ImportedItem, Opportunity
from opportunities.services.gmail_source import (
    INBOX_URL, get_account, message_permalink, validate_message_identity,
)
from opportunities.services.importing import import_candidates, promote_imported_item
from opportunities.services.source_identity import normalize_source_identity


ACCOUNT_EMAIL = "student@example.edu"
EXTERNAL_ID = "18f0a1b2c3d4e5f6"


def gmail_candidate(**overrides):
    values = {
        "external_id": EXTERNAL_ID, "source_url": message_permalink(EXTERNAL_ID),
        "title": "Research internship", "organization": "Example University",
        "type": Opportunity.Type.INTERNSHIP, "description": "Original email announcement.",
        "raw_data": {"subject": "Research internship", "text": "Original message"},
    }
    values.update(overrides)
    return values


@override_settings(GMAIL_ACCOUNT_EMAIL=ACCOUNT_EMAIL)
class GmailSourceValidationTests(SimpleTestCase):
    def test_account_identity_is_normalized_stable_immutable_and_hides_address_in_key(self):
        account = get_account()
        expected_key = "gmail-" + hashlib.sha256(ACCOUNT_EMAIL.encode("utf-8")).hexdigest()[:40]
        self.assertEqual(account.email, ACCOUNT_EMAIL)
        self.assertEqual(account.source_key, expected_key)
        self.assertLessEqual(len(account.source_key), ImportedItem._meta.get_field("source_key").max_length)
        self.assertNotIn(ACCOUNT_EMAIL, account.source_key)
        with override_settings(GMAIL_ACCOUNT_EMAIL="  STUDENT@EXAMPLE.EDU  "):
            self.assertEqual(get_account(), account)
        with self.assertRaises(AttributeError):
            account.email = "another@example.edu"

    def test_missing_or_invalid_account_is_rejected_before_identity_can_be_used(self):
        for configured in ("", " ", None, [], "not-an-email", "student@", "Name <student@example.edu>"):
            with self.subTest(configured=configured), override_settings(GMAIL_ACCOUNT_EMAIL=configured):
                for operation in (get_account, lambda: message_permalink(EXTERNAL_ID)):
                    with self.assertRaises(ValueError):
                        operation()

    def test_canonical_message_identity_has_no_email_or_account_selector(self):
        url = INBOX_URL + "/" + EXTERNAL_ID
        self.assertEqual(message_permalink(EXTERNAL_ID), url)
        self.assertNotIn(ACCOUNT_EMAIL, url)
        self.assertEqual(validate_message_identity(EXTERNAL_ID, url), (EXTERNAL_ID, url))
        self.assertEqual(
            normalize_source_identity("gmail", get_account().source_key, EXTERNAL_ID, url),
            (EXTERNAL_ID, url),
        )

    def test_message_id_requires_exact_legacy_lowercase_hex_without_coercion(self):
        for external_id in (
            None, 1234567890123456, "", "18f0a1b2c3d4e5f", "18f0a1b2c3d4e5f67",
            EXTERNAL_ID.upper(), "g" * 16, EXTERNAL_ID + "\n", " " + EXTERNAL_ID,
            "thread-f:" + EXTERNAL_ID, "FMfcgzExampleOpaqueId", "１８f０a１b２c３d４e５f６",
        ):
            with self.subTest(external_id=external_id), self.assertRaises(ValueError):
                message_permalink(external_id)

    def test_url_must_match_exact_host_account_slot_inbox_and_message(self):
        canonical = message_permalink(EXTERNAL_ID)
        for url in (
            canonical.replace("https:", "http:"),
            canonical.replace("mail.google.com", "mail.google.com.attacker.test"),
            canonical.replace("https://", "https://user:password@"),
            canonical.replace("mail.google.com", "mail.google.com:443"),
            canonical.replace("/u/0/", "/u/1/"),
            canonical.replace("#inbox/", "#all/"),
            canonical.replace(EXTERNAL_ID, "0" * 16),
            canonical.replace("/#", "/?authuser=student@example.edu#"),
            canonical + "?", canonical + "#", canonical + "/", canonical + "\n",
            canonical.replace("inbox/", "inbox%2F"), None,
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_message_identity(EXTERNAL_ID, url)

    def test_configured_account_key_cannot_be_forged_or_omitted_by_import_dispatch(self):
        url = message_permalink(EXTERNAL_ID)
        with override_settings(GMAIL_ACCOUNT_EMAIL="other@example.edu"):
            another_key = get_account().source_key
        self.assertNotEqual(another_key, get_account().source_key)
        for key in (another_key, ACCOUNT_EMAIL, "gmail-arbitrary", "", None):
            with self.subTest(source_key=key), self.assertRaises(ValueError):
                normalize_source_identity("gmail", key, EXTERNAL_ID, url)
        with self.assertRaises(ValueError):
            validate_message_identity(EXTERNAL_ID, url, source_key=another_key)


@override_settings(GMAIL_ACCOUNT_EMAIL=ACCOUNT_EMAIL)
class GmailPersistenceTests(TestCase):
    def import_gmail(self, *candidates):
        return import_candidates(candidates, source_key=get_account().source_key, source=Opportunity.Source.GMAIL)

    def test_complete_candidate_creates_gmail_draft_without_obeying_source_publication_flags(self):
        report = self.import_gmail(gmail_candidate(source="manual", status="published", allow_missing_details=True))
        self.assertEqual(report["created"], 1)
        self.assertEqual(report["errors"], 0)
        item = ImportedItem.objects.get()
        opportunity = item.opportunity
        self.assertEqual(item.source, Opportunity.Source.GMAIL)
        self.assertEqual(item.source_key, get_account().source_key)
        self.assertEqual(opportunity.source, Opportunity.Source.GMAIL)
        self.assertEqual(opportunity.status, Opportunity.Status.DRAFT)
        self.assertFalse(opportunity.allow_missing_details)
        self.assertEqual(opportunity.source_url, message_permalink(EXTERNAL_ID))
        item.full_clean()
        opportunity.full_clean()

    def test_repeated_message_deduplicates_and_source_edits_preserve_staff_values(self):
        self.import_gmail(gmail_candidate())
        self.assertEqual(self.import_gmail(gmail_candidate())["unchanged"], 1)
        item = ImportedItem.objects.get()
        item.organization = "Reviewed employer"
        item.notes = "Staff notes"
        item.save()
        Opportunity.objects.filter(pk=item.opportunity_id).update(title="Published staff title", status="published")
        report = self.import_gmail(gmail_candidate(title="Edited email title", organization="Changed source employer"))
        self.assertEqual(report["changed"], 1)
        item.refresh_from_db()
        self.assertEqual(item.organization, "Reviewed employer")
        self.assertEqual(item.notes, "Staff notes")
        self.assertEqual(item.source_payload["title"], "Edited email title")
        self.assertTrue(item.review_required)
        self.assertEqual(item.opportunity.title, "Published staff title")
        self.assertEqual(item.opportunity.status, Opportunity.Status.PUBLISHED)
        self.assertEqual(Opportunity.objects.count(), 1)

    def test_incomplete_email_stays_staged_and_corrected_promotion_keeps_gmail_provenance(self):
        report = self.import_gmail(gmail_candidate(organization="", allow_missing_details=True))
        self.assertEqual(report["staged"], 1)
        self.assertFalse(Opportunity.objects.exists())
        item = ImportedItem.objects.get()
        item.organization = "Reviewed employer"
        item.save()
        opportunity = promote_imported_item(item.pk)
        self.assertEqual(opportunity.source, Opportunity.Source.GMAIL)
        self.assertEqual(opportunity.status, Opportunity.Status.DRAFT)
        self.assertEqual(opportunity.source_url, message_permalink(EXTERNAL_ID))
        self.assertIsNone(promote_imported_item(item.pk))

    def test_wrong_account_key_or_message_url_is_rejected_without_writing(self):
        report = import_candidates(
            [gmail_candidate()], source_key="gmail-unapproved-account", source=Opportunity.Source.GMAIL,
        )
        self.assertEqual(report["errors"], 1)
        report = self.import_gmail(gmail_candidate(source_url=INBOX_URL + "/" + "0" * 16))
        self.assertEqual(report["errors"], 1)
        self.assertFalse(ImportedItem.objects.exists())
        self.assertFalse(Opportunity.objects.exists())

    def test_same_message_id_in_two_explicit_account_configurations_has_distinct_identity(self):
        self.import_gmail(gmail_candidate())
        original_key = get_account().source_key
        with override_settings(GMAIL_ACCOUNT_EMAIL="other@example.edu"):
            self.assertEqual(self.import_gmail(gmail_candidate())["created"], 1)
            another_key = get_account().source_key
        self.assertNotEqual(original_key, another_key)
        self.assertEqual(ImportedItem.objects.count(), 2)
        self.assertEqual(Opportunity.objects.count(), 2)
        self.assertEqual(self.import_gmail(gmail_candidate())["unchanged"], 1)

    def test_model_validation_rejects_gmail_provenance_after_account_is_changed(self):
        self.import_gmail(gmail_candidate(organization=""))
        item = ImportedItem.objects.get()
        with override_settings(GMAIL_ACCOUNT_EMAIL="other@example.edu"):
            with self.assertRaises(ValidationError):
                item.full_clean()
            with self.assertRaises(ValidationError):
                promote_imported_item(item.pk)
        self.assertFalse(Opportunity.objects.exists())
        self.assertEqual(Opportunity.Source.EMAIL, "email")
