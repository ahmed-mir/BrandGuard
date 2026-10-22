import uuid

from django import forms

from .models import ContentSubmission


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
