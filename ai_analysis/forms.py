import uuid

from django import forms


class AIAnalysisConsentForm(forms.Form):
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, demo_mode=False, provider_label="OpenAI", **kwargs):
        kwargs.setdefault("initial", {"idempotency_key": uuid.uuid4()})
        super().__init__(*args, **kwargs)
        if demo_mode:
            self.fields["confirm_demo_simulation"] = forms.BooleanField(
                required=True,
                label="I understand this is a simulated demo response and no external AI provider will be contacted.",
                error_messages={"required": "Confirm the Demo AI disclosure before running the simulated analysis."},
            )
        else:
            self.fields["consent_external_processing"] = forms.BooleanField(
                required=True,
                label=f"I authorize sending this content and the selected approved source excerpts to {provider_label} for this analysis.",
                error_messages={"required": "Confirm the external-processing disclosure before running AI analysis."},
            )
