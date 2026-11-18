from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.core.validators import URLValidator
from django.db.models import F, Q
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from .forms import OpportunityFilterForm
from .models import Opportunity


def published_opportunities():
    # Staff use the same public scope; drafts remain available only in admin.
    return Opportunity.objects.filter(status=Opportunity.Status.PUBLISHED)


@require_GET
def opportunity_list(request):
    form = OpportunityFilterForm(request.GET)
    queryset = published_opportunities()
    if form.is_valid():
        filters = form.cleaned_data
        if filters["q"]:
            queryset = queryset.filter(
                Q(title__icontains=filters["q"])
                | Q(organization__icontains=filters["q"])
                | Q(description__icontains=filters["q"])
            )
        if filters["type"]:
            queryset = queryset.filter(type=filters["type"])
        if filters["event_type"]:
            queryset = queryset.filter(type=Opportunity.Type.EVENT, event_type=filters["event_type"])
        if filters["source"]:
            queryset = queryset.filter(source=filters["source"])
        if filters["hide_seminars"]:
            queryset = queryset.exclude(type=Opportunity.Type.EVENT, event_type=Opportunity.EventType.SEMINAR)
        if filters["sort"] == "deadline":
            queryset = queryset.order_by(F("deadline").asc(nulls_last=True), "-created_at", "-pk")
        else:
            queryset = queryset.order_by("-created_at", "-pk")
    else:
        queryset = queryset.none()

    page_obj = Paginator(queryset, 12).get_page(request.GET.get("page"))
    preserved = form.data.copy()
    for key in list(preserved):
        if key not in form.fields or not preserved.get(key):
            preserved.pop(key)
    return render(request, "opportunities/list.html", {
        "form": form,
        "page_obj": page_obj,
        "opportunities": page_obj.object_list,
        "result_count": page_obj.paginator.count,
        "query_string": preserved.urlencode(),
        "has_filters": any(preserved.get(key) for key in ("q", "type", "event_type", "source", "hide_seminars")),
    })


def safe_source_url(value):
    try:
        URLValidator(schemes=["http", "https"])(value)
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            return ""
    except (ValidationError, ValueError, TypeError):
        return ""
    return value


@require_GET
def opportunity_detail(request, pk):
    opportunity = get_object_or_404(published_opportunities(), pk=pk)
    return render(request, "opportunities/detail.html", {
        "opportunity": opportunity,
        "original_url": safe_source_url(opportunity.source_url) if opportunity.source_url else "",
    })
