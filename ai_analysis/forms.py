import uuid

from django import forms


class AIAnalysisConsentForm(forms.Form):
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput)
    consent_external_processing = forms.BooleanField(
        required=True,
        label="I authorize sending this content and the selected approved source excerpts to the external AI provider for this analysis.",
        error_messages={"required": "Confirm the external-processing disclosure before running AI analysis."},
    )

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("initial", {"idempotency_key": uuid.uuid4()})
        super().__init__(*args, **kwargs)
