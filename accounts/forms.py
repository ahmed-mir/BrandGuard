from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.forms import PasswordChangeForm
from django.utils.translation import gettext_lazy as _
from django import forms
from django.contrib.auth import get_user_model


class UsernameOrEmailAuthenticationForm(AuthenticationForm):
    """Standard Django authentication form with an honest identifier label."""

    username = forms.CharField(
        label=_("Username or email"),
        widget=forms.TextInput(
            attrs={
                "autofocus": True,
                "autocomplete": "username",
                "class": "form-control",
                "placeholder": "you or your work email",
                "spellcheck": "false",
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.error_messages["invalid_login"] = _(
            "Those details do not match an active account. Check them and try again."
        )
        self.fields["password"].widget.attrs.update(
            {"autocomplete": "current-password", "class": "form-control"}
        )


class AccountProfileForm(forms.ModelForm):
    """Allow users to update only their own non-privileged name fields."""

    class Meta:
        model = get_user_model()
        fields = ("first_name", "last_name")
        widgets = {
            "first_name": forms.TextInput(
                attrs={"class": "form-control", "autocomplete": "given-name"}
            ),
            "last_name": forms.TextInput(
                attrs={"class": "form-control", "autocomplete": "family-name"}
            ),
        }
        labels = {"first_name": "First name", "last_name": "Last name"}


class AccountPasswordChangeForm(PasswordChangeForm):
    """Django's current-password and validated new-password form with shared styling."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["old_password"].label = "Current password"
        for name, field in self.fields.items():
            field.widget.attrs.pop("autofocus", None)
            field.widget.attrs.update(
                {
                    "class": "form-control",
                    "autocomplete": (
                        "current-password" if name == "old_password" else "new-password"
                    ),
                }
            )


def mark_form_errors_for_accessibility(form):
    """Associate server-side validation messages with their fields."""
    for bound_field in form:
        if not bound_field.errors:
            continue
        described_by = []
        if bound_field.help_text:
            described_by.append(f"{bound_field.auto_id}_helptext")
        described_by.append(f"{bound_field.auto_id}-errors")
        bound_field.field.widget.attrs.update(
            {
                "aria-invalid": "true",
                "aria-describedby": " ".join(described_by),
            }
        )
