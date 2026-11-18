from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import HttpResponseRedirect
from django.urls import path, reverse
from django.utils.decorators import method_decorator
from django.utils.html import format_html
from django.views.decorators.http import require_POST

from .models import ImportedItem, Opportunity
from .services.importing import promote_imported_item
from .services.publication import publish_imported_item, publish_opportunity


def validation_details(error):
    if not hasattr(error, "message_dict"):
        return "; ".join(error.messages)
    labels = {"type": "opportunity type", "__all__": "record"}
    return "; ".join(
        f"{labels.get(name, name.replace('_', ' '))}: " + ", ".join(errors)
        for name, errors in error.message_dict.items()
    )


def publication_feedback(model_admin, request, *, published, created, already_published, archived, errors):
    noun = "opportunity" if published == 1 else "opportunities"
    summary = f"Published {published} {noun}."
    if created:
        summary += f" Created {created} from imported items."
    if already_published:
        summary += f" Skipped {already_published} already-published {'item' if already_published == 1 else 'items'}."
    if archived:
        summary += f" Skipped {archived} archived {'item' if archived == 1 else 'items'}."
    if errors:
        summary += f" {len(errors)} {'item needs' if len(errors) == 1 else 'items need'} corrections. "
        summary += " | ".join(errors[:5])
        if len(errors) > 5:
            summary += f" | {len(errors) - 5} more items need corrections."
    model_admin.message_user(request, summary, level=messages.WARNING if errors else messages.SUCCESS)


class HideSeminarsFilter(admin.SimpleListFilter):
    title = "Seminars"
    parameter_name = "hide_seminars"

    def lookups(self, request, model_admin):
        return (("1", "Hide seminars"),)

    def queryset(self, request, queryset):
        if self.value() == "1":
            return queryset.exclude(type=Opportunity.Type.EVENT, event_type=Opportunity.EventType.SEMINAR)
        return queryset


@admin.register(Opportunity)
class OpportunityAdmin(admin.ModelAdmin):
    change_list_template = "admin/opportunities/opportunity/change_list.html"
    list_display = (
        "title",
        "organization",
        "opportunity_type",
        "event_type",
        "employment_type",
        "work_mode",
        "status",
        "has_missing_details",
        "deadline",
        "source",
        "created_at",
    )
    list_filter = ("type", "event_type", HideSeminarsFilter, "employment_type", "work_mode", "status", "source")
    search_fields = ("title", "organization", "description")
    readonly_fields = ("created_at",)
    actions = ("publish_selected_drafts",)
    date_hierarchy = "created_at"
    fieldsets = (
        (
            "Opportunity",
            {
                "fields": (
                    "title",
                    "organization",
                    "type",
                    "event_type",
                    "employment_type",
                    "work_mode",
                    "description",
                    "location",
                    "deadline",
                )
            },
        ),
        ("Publication", {"fields": ("status", "allow_missing_details", "created_at")}),
        ("Source", {"fields": ("source", "source_url")}),
    )

    def get_urls(self):
        return [
            path(
                "publish-all-drafts/",
                self.admin_site.admin_view(self.publish_all_drafts_view),
                name="opportunities_opportunity_publish_all_drafts",
            ),
        ] + super().get_urls()

    def changelist_view(self, request, extra_context=None):
        context = dict(extra_context or {})
        context["can_publish_all_drafts"] = self.has_change_permission(request)
        context["publish_all_drafts_count"] = (
            self.get_queryset(request).filter(status=Opportunity.Status.DRAFT).count()
            if context["can_publish_all_drafts"] else 0
        )
        return super().changelist_view(request, extra_context=context)

    @method_decorator(require_POST)
    def publish_all_drafts_view(self, request):
        if not self.has_change_permission(request):
            raise PermissionDenied
        with transaction.atomic():
            drafts = list(
                self.get_queryset(request).select_for_update().filter(status=Opportunity.Status.DRAFT)
            )
            count = self.get_queryset(request).filter(
                pk__in=[draft.pk for draft in drafts], status=Opportunity.Status.DRAFT,
            ).update(status=Opportunity.Status.PUBLISHED)
            for draft in drafts:
                self.log_change(request, draft, [{"changed": {"fields": ["status"]}}])
        if count:
            noun = "opportunity" if count == 1 else "opportunities"
            self.message_user(request, f"Published {count} draft {noun}.", level=messages.SUCCESS)
        else:
            self.message_user(request, "There are no draft opportunities to publish.", level=messages.INFO)
        return HttpResponseRedirect(reverse("admin:opportunities_opportunity_changelist"))

    @admin.display(description="Opportunity type", ordering="type")
    def opportunity_type(self, opportunity):
        return opportunity.get_type_display()

    @admin.display(boolean=True, description="Missing details")
    def has_missing_details(self, opportunity):
        return opportunity.has_missing_details

    def has_publish_incomplete_permission(self, request):
        return self.admin_site.has_permission(request) and self.has_change_permission(request)

    @admin.action(
        description="Publish selected drafts (allow missing details)",
        permissions=["publish_incomplete"],
    )
    @method_decorator(require_POST)
    def publish_selected_drafts(self, request, queryset):
        if not self.has_publish_incomplete_permission(request):
            raise PermissionDenied
        published = created = already_published = archived = 0
        errors = []
        for opportunity in queryset:
            try:
                with transaction.atomic():
                    result = publish_opportunity(opportunity.pk, allow_missing_details=True)
                    if result.published:
                        changed_fields = ["status"]
                        if result.opportunity.allow_missing_details != opportunity.allow_missing_details:
                            changed_fields.append("allow_missing_details")
                        self.log_change(request, result.opportunity, [{"changed": {"fields": changed_fields}}])
            except ValidationError as exc:
                errors.append(f"{opportunity}: {validation_details(exc)}")
            else:
                published += result.published
                created += result.created
                already_published += result.reason == "already_published"
                archived += result.reason == "archived"
        publication_feedback(
            self, request, published=published, created=created,
            already_published=already_published, archived=archived, errors=errors,
        )

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "type":
            kwargs["label"] = "Opportunity type"
        return super().formfield_for_dbfield(db_field, request, **kwargs)


@admin.register(ImportedItem)
class ImportedItemAdmin(admin.ModelAdmin):
    list_display = (
        "title", "organization", "opportunity_type", "event_type", "employment_type", "work_mode",
        "source", "review_required", "created_opportunity", "last_seen",
    )
    list_select_related = ("opportunity",)
    list_filter = ("review_required", "source", "source_key", "type", "event_type", HideSeminarsFilter, "employment_type", "work_mode")
    search_fields = ("title", "organization", "external_id", "notes")
    readonly_fields = (
        "source", "source_key", "external_id", "source_url", "source_payload", "source_hash",
        "created_opportunity", "draft_readiness", "first_seen", "last_seen",
    )
    actions = ("create_drafts", "publish_selected_imports")
    fieldsets = (
        ("Review extracted details", {"fields": (
            "title", "organization", "type", "event_type", "employment_type", "work_mode",
            "description", "location", "deadline",
            "notes", "review_required", "created_opportunity", "draft_readiness",
        )}),
        ("Latest source snapshot", {"fields": (
            "source_url", "source_payload", "source_hash",
        )}),
        ("Import identity", {"fields": (
            "source", "source_key", "external_id", "first_seen", "last_seen",
        )}),
    )

    def has_add_permission(self, request):
        return False

    def has_publish_incomplete_permission(self, request):
        return (
            self.admin_site.has_permission(request)
            and self.has_change_permission(request)
            and request.user.has_perm("opportunities.add_opportunity")
            and request.user.has_perm("opportunities.change_opportunity")
        )

    @admin.display(description="Opportunity type", ordering="type")
    def opportunity_type(self, item):
        return item.get_type_display() or "Needs review"

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        required_help = {
            "title": "Required to create a draft opportunity.",
            "organization": (
                "Required to create a draft opportunity. Enter the employer or organizer "
                "named in the original posting. The publish-selected action can explicitly allow missing details."
            ),
            "type": (
                "Required to create a draft opportunity: Internship, Job, or Event. "
                "Working hours and remote/on-site arrangements are separate fields. "
                "The publish-selected action can explicitly allow missing details."
            ),
            "employment_type": "Working hours from the source, such as full-time or part-time.",
            "event_type": "Optional format for events, such as Seminar or Workshop. Leave blank for jobs and internships.",
            "work_mode": "Where the work takes place: remote, hybrid, or on-site.",
        }
        for field_name, help_text in required_help.items():
            if field_name in form.base_fields:
                form.base_fields[field_name].help_text = help_text
        if "type" in form.base_fields:
            form.base_fields["type"].label = "Opportunity type"
        return form

    @staticmethod
    def missing_draft_fields(item):
        labels = (("title", "title"), ("organization", "organization"), ("type", "opportunity type"))
        return [label for name, label in labels if not getattr(item, name).strip()]

    @admin.display(description="Created opportunity")
    def created_opportunity(self, item):
        if item.opportunity_id:
            opportunity = item.opportunity
            return format_html(
                '<a href="{}">{} ({})</a>',
                reverse("admin:opportunities_opportunity_change", args=[opportunity.pk]),
                opportunity.title,
                opportunity.get_status_display(),
            )
        missing = self.missing_draft_fields(item)
        if missing:
            return "Not created — complete " + ", ".join(missing)
        return "Not created — ready to create draft"

    @admin.display(description="Draft creation")
    def draft_readiness(self, item):
        if item.opportunity_id:
            return (
                "An opportunity has already been created. Use its link above to review or "
                "edit it; changing this imported item does not update that opportunity."
            )
        missing = self.missing_draft_fields(item)
        if missing:
            return (
                "Complete these required fields: " + ", ".join(missing) + ". "
                "Save this item, then select it in the Imported items list and choose "
                "Create draft opportunities from selected imported items. "
                "To publish with missing organization or opportunity type, select the item and choose "
                "Publish selected imports (allow missing details). A title is always required."
            )
        return (
            "Ready to create a draft. Save any corrections, then select this item in the "
            "Imported items list and choose Create draft opportunities from selected "
            "imported items."
        )

    @staticmethod
    def validation_details(error):
        return validation_details(error)

    @admin.action(
        description="Publish selected imports (allow missing details)",
        permissions=["publish_incomplete"],
    )
    @method_decorator(require_POST)
    def publish_selected_imports(self, request, queryset):
        if not self.has_publish_incomplete_permission(request):
            raise PermissionDenied
        opportunity_admin = self.admin_site._registry[Opportunity]
        published = created = already_published = archived = 0
        errors = []
        for item in queryset.select_related("opportunity"):
            previous_allow_missing = item.opportunity.allow_missing_details if item.opportunity_id else False
            try:
                with transaction.atomic():
                    result = publish_imported_item(item.pk, allow_missing_details=True)
                    if result.created:
                        opportunity_admin.log_addition(request, result.opportunity, [{"added": {}}])
                        self.log_change(request, item, [{"changed": {"fields": ["opportunity"]}}])
                    if result.published:
                        changed_fields = ["status"]
                        if result.opportunity.allow_missing_details != previous_allow_missing:
                            changed_fields.append("allow_missing_details")
                        opportunity_admin.log_change(request, result.opportunity, [{"changed": {"fields": changed_fields}}])
            except ValidationError as exc:
                errors.append(f"{item}: {validation_details(exc)}")
            else:
                published += result.published
                created += result.created
                already_published += result.reason == "already_published"
                archived += result.reason == "archived"
        publication_feedback(
            self, request, published=published, created=created,
            already_published=already_published, archived=archived, errors=errors,
        )

    @admin.action(
        description="Create draft opportunities from selected imported items",
        permissions=["change"],
    )
    def create_drafts(self, request, queryset):
        if not self.has_change_permission(request) or not request.user.has_perm("opportunities.add_opportunity"):
            self.message_user(
                request, "You need permission to change imported items and add opportunities.",
                level=messages.ERROR,
            )
            return
        created = 0
        already_created = 0
        incomplete = []
        for item in queryset:
            try:
                opportunity = promote_imported_item(item.pk)
            except ValidationError as exc:
                incomplete.append(f"{item}: {self.validation_details(exc)}")
            else:
                created += opportunity is not None
                already_created += opportunity is None
        summary = f"Created {created} draft {'opportunity' if created == 1 else 'opportunities'}."
        if already_created:
            summary += (
                f" Skipped {already_created} already-created "
                f"{'item' if already_created == 1 else 'items'}."
            )
        if incomplete:
            summary += (
                f" {len(incomplete)} {'item needs' if len(incomplete) == 1 else 'items need'} "
                "corrections. Complete the named fields, "
                "save each imported item, then run this action again. "
                + " | ".join(incomplete[:5])
            )
            if len(incomplete) > 5:
                summary += f" | {len(incomplete) - 5} more items need corrections."
        self.message_user(
            request, summary, level=messages.WARNING if incomplete else messages.SUCCESS,
        )
