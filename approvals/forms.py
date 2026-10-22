from django import forms


class RequestChangesForm(forms.Form):
    comment = forms.CharField(
        min_length=10,
        max_length=3000,
        label="Changes requested",
        help_text="Explain what should change so the creator can prepare a new version.",
        widget=forms.Textarea(attrs={"rows": 5, "maxlength": 3000}),
    )

    def clean_comment(self):
        value = self.cleaned_data["comment"].strip()
        if len(value) < 10:
            raise forms.ValidationError("Provide a useful explanation of at least 10 characters.")
        return value


class ApproveVersionForm(forms.Form):
    override_reason = forms.CharField(
        required=False,
        max_length=3000,
        label="High-severity override reason",
        help_text="Required when approving with unresolved high-severity findings. Findings remain open; document why approval is appropriate.",
        widget=forms.Textarea(attrs={"rows": 4, "maxlength": 3000}),
    )

    def clean_override_reason(self):
        return self.cleaned_data["override_reason"].strip()
