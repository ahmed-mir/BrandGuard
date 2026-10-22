import uuid

from django import forms

from .models import ContentSubmission
from .models import ReviewRun


MAX_CONTENT_CHARACTERS = 20_000


class ContentSubmissionForm(forms.Form):
    title = forms.CharField(
        max_length=160,
        label="Submission title",
        widget=forms.TextInput(attrs={"maxlength": 160, "autocomplete": "off"}),
    )
    channel = forms.ChoiceField(choices=ContentSubmission.Channel.choices, label="Content channel")
    audience = forms.CharField(
        max_length=200,
        required=False,
        label="Intended audience (optional)",
        widget=forms.TextInput(attrs={"maxlength": 200}),
    )
    campaign_purpose = forms.CharField(
        max_length=300,
        required=False,
        label="Campaign purpose (optional)",
        widget=forms.TextInput(attrs={"maxlength": 300}),
    )
    content_text = forms.CharField(
        max_length=MAX_CONTENT_CHARACTERS,
        error_messages={"required": "Paste the marketing text you want checked."},
        label="Marketing content",
        widget=forms.Textarea(attrs={
            "rows": 16,
            "maxlength": MAX_CONTENT_CHARACTERS,
            "autocomplete": "off",
            "spellcheck": "true",
        }),
        help_text=f"Paste plain-text content. Up to {MAX_CONTENT_CHARACTERS:,} characters. This phase does not accept campaign file uploads.",
    )

    def clean_title(self):
        title = self.cleaned_data["title"].strip()
        if not title:
            raise forms.ValidationError("Enter a title for this submission.")
        return title

    def clean_content_text(self):
        content = self.cleaned_data["content_text"]
        if not content.strip():
            raise forms.ValidationError("Paste the marketing text you want checked.")
        return content


class StartReviewForm(forms.Form):
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("initial", {"idempotency_key": uuid.uuid4()})
        super().__init__(*args, **kwargs)


class ContentRevisionForm(forms.Form):
    content_text = forms.CharField(
        max_length=MAX_CONTENT_CHARACTERS,
        label="Revised marketing content",
        widget=forms.Textarea(attrs={
            "rows": 18,
            "maxlength": MAX_CONTENT_CHARACTERS,
            "spellcheck": "true",
        }),
    )
    base_version_id = forms.UUIDField(widget=forms.HiddenInput)
    base_content_hash = forms.CharField(max_length=64, widget=forms.HiddenInput)
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput)

    def clean_content_text(self):
        content = self.cleaned_data["content_text"]
        if not content.strip():
            raise forms.ValidationError("Paste the revised marketing text before saving a new version.")
        return content


class SubmitForApprovalForm(forms.Form):
    review_run_id = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, content_version=None, **kwargs):
        super().__init__(*args, **kwargs)
        if content_version is not None:
            self.review_runs = ReviewRun.objects.filter(
                content_version=content_version,
                status=ReviewRun.Status.COMPLETE,
            ).order_by("-started_at")

    def clean_review_run_id(self):
        review_id = self.cleaned_data["review_run_id"]
        if not hasattr(self, "review_runs") or not self.review_runs.filter(pk=review_id).exists():
            raise forms.ValidationError("Choose a completed review for this exact content version.")
        return review_id
