import uuid

from django import forms

from .models import ReviewFinding


class ReviewCommentForm(forms.Form):
    body = forms.CharField(
        max_length=3000,
        label="Add to the discussion",
        widget=forms.Textarea(attrs={"rows": 3, "maxlength": 3000}),
    )
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("initial", {"idempotency_key": uuid.uuid4()})
        super().__init__(*args, **kwargs)

    def clean_body(self):
        body = self.cleaned_data["body"].strip()
        if not body:
            raise forms.ValidationError("Enter a comment before posting.")
        return body


class FindingResolutionForm(forms.Form):
    expected_status = forms.ChoiceField(
        choices=ReviewFinding.ResolutionStatus.choices,
        widget=forms.HiddenInput,
    )
    new_status = forms.ChoiceField(label="Update finding status")
    note = forms.CharField(
        max_length=1000,
        required=False,
        label="Note (required for Resolved or Won't fix)",
        widget=forms.TextInput(attrs={"maxlength": 1000}),
    )
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, current_status, choices, **kwargs):
        initial = kwargs.pop("initial", {})
        initial.setdefault("expected_status", current_status)
        initial.setdefault("idempotency_key", uuid.uuid4())
        super().__init__(*args, initial=initial, **kwargs)
        self.fields["new_status"].choices = choices

    def clean_note(self):
        return self.cleaned_data["note"].strip()\n