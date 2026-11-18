from django.core.validators import URLValidator
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class Opportunity(models.Model):
    class Type(models.TextChoices):
        INTERNSHIP = "internship", "Internship"
        JOB = "job", "Job"
        EVENT = "event", "Event"

    class EventType(models.TextChoices):
        SEMINAR = "seminar", "Seminar"
        LECTURE = "lecture", "Lecture"
        WORKSHOP = "workshop", "Workshop"
        CONFERENCE = "conference", "Conference"
        COMPETITION = "competition", "Competition"
        OTHER = "other", "Other event"

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual"
        NOTION = "notion", "Notion"
        SLACK = "slack", "Slack"
        GMAIL = "gmail", "Gmail"
        EMAIL = "email", "Email"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        ARCHIVED = "archived", "Archived"

    class EmploymentType(models.TextChoices):
        FULL_TIME = "full_time", "Full-time"
        PART_TIME = "part_time", "Part-time"

    class WorkMode(models.TextChoices):
        REMOTE = "remote", "Remote / online"
        HYBRID = "hybrid", "Hybrid"
        ON_SITE = "on_site", "On-site / in person"

    title = models.CharField(max_length=200)
    organization = models.CharField(max_length=200, blank=True)
    type = models.CharField(max_length=20, choices=Type.choices, blank=True)
    event_type = models.CharField(
        "Event format", max_length=20, choices=EventType.choices, blank=True, default="",
    )
    employment_type = models.CharField(max_length=20, choices=EmploymentType.choices, blank=True)
    work_mode = models.CharField(max_length=20, choices=WorkMode.choices, blank=True)
    description = models.TextField(blank=True)
    location = models.CharField(
        max_length=200, blank=True,
        help_text="Geographic location or explicit applicant location requirement; work arrangement is recorded separately.",
    )
    deadline = models.DateField(null=True, blank=True)
    source = models.CharField(
        max_length=20, choices=Source.choices, default=Source.MANUAL
    )
    source_url = models.URLField(
        max_length=500,
        blank=True,
        validators=[URLValidator(schemes=["http", "https"])],
        help_text="Optional link to the original posting. HTTP or HTTPS only.",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    allow_missing_details = models.BooleanField(
        default=False,
        help_text=(
            "Allow organization or opportunity type to be missing after a staff review. "
            "A title and all other validation are still required."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def clean(self):
        super().clean()
        errors = {}
        if isinstance(self.title, str) and self.title and not self.title.strip():
            errors["title"] = "This field cannot be blank."
        if not self.allow_missing_details:
            for field in ("organization", "type"):
                value = getattr(self, field)
                if value is None or (isinstance(value, str) and not value.strip()):
                    errors[field] = "This field cannot be blank."
        if self.event_type and self.type != self.Type.EVENT:
            errors["event_type"] = "Event format is only available for event opportunities."
        if errors:
            raise ValidationError(errors)

    @property
    def missing_details(self):
        return tuple(
            label for field, label in (("organization", "organization"), ("type", "opportunity type"))
            if (value := getattr(self, field)) is None or (isinstance(value, str) and not value.strip())
        )

    @property
    def has_missing_details(self):
        return bool(self.missing_details)

    def __str__(self):
        return self.title


class ImportedItem(models.Model):
    """Source snapshots and reviewer corrections are intentionally separate."""

    source = models.CharField(
        max_length=20, choices=Opportunity.Source.choices, default=Opportunity.Source.NOTION
    )
    source_key = models.CharField(max_length=64)
    external_id = models.CharField(max_length=32)
    source_url = models.URLField(max_length=500)
    source_payload = models.JSONField(default=dict)
    source_hash = models.CharField(max_length=64)
    title = models.CharField(max_length=200, blank=True)
    organization = models.CharField(max_length=200, blank=True)
    type = models.CharField(max_length=20, choices=Opportunity.Type.choices, blank=True)
    event_type = models.CharField(
        "Event format", max_length=20, choices=Opportunity.EventType.choices, blank=True, default="",
    )
    employment_type = models.CharField(max_length=20, choices=Opportunity.EmploymentType.choices, blank=True)
    work_mode = models.CharField(max_length=20, choices=Opportunity.WorkMode.choices, blank=True)
    description = models.TextField(blank=True)
    location = models.CharField(
        max_length=200, blank=True,
        help_text="City/country from the source. Internship and part-time are not locations.",
    )
    deadline = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    review_required = models.BooleanField(default=True)
    opportunity = models.OneToOneField(
        Opportunity,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="imported_item",
        help_text="The opportunity record created from this import. Blank means no draft has been created yet.",
    )
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-first_seen", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["source_key", "external_id"], name="unique_imported_source_item"
            )
        ]

    def clean(self):
        super().clean()
        if self.event_type and self.type != Opportunity.Type.EVENT:
            raise ValidationError({"event_type": "Event format is only available for event opportunities."})
        from .services.source_identity import normalize_source_identity

        try:
            self.external_id, self.source_url = normalize_source_identity(
                self.source, self.source_key, self.external_id, self.source_url,
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    def __str__(self):
        return self.title or self.external_id
