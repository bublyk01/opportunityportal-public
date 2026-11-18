from django import forms

from .models import Opportunity


class OpportunityFilterForm(forms.Form):
    q = forms.CharField(
        required=False, max_length=200, label="Search",
        widget=forms.TextInput(attrs={
            "placeholder": "Search roles, organizations, or keywords",
            "type": "search",
        }),
    )
    type = forms.ChoiceField(
        required=False, label="Opportunity type",
        choices=(("", "All opportunities"), *Opportunity.Type.choices),
    )
    event_type = forms.ChoiceField(
        required=False, label="Event format",
        choices=(("", "All event formats"), *Opportunity.EventType.choices),
    )
    source = forms.ChoiceField(
        required=False, label="Source",
        choices=(("", "All sources"), *Opportunity.Source.choices),
    )
    sort = forms.ChoiceField(
        required=False, label="Sort by", initial="newest",
        choices=(("newest", "Newest first"), ("deadline", "Deadline: earliest first")),
    )
    hide_seminars = forms.BooleanField(
        required=False, label="Hide seminars", widget=forms.CheckboxInput(attrs={"value": "1"}),
    )

    def __init__(self, data=None, **kwargs):
        if data is not None and data.get("type") in (
            Opportunity.Type.INTERNSHIP, Opportunity.Type.JOB,
        ):
            data = data.copy()
            # A format selected for events must not empty a later internship search.
            data["event_type"] = ""
        super().__init__(data=data, **kwargs)
