from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.views import LoginView, LogoutView
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from workspaces.page_context import app_page_context
from workspaces.permissions import workspace_member_required

from .forms import (
    AccountPasswordChangeForm,
    AccountProfileForm,
    UsernameOrEmailAuthenticationForm,
    mark_form_errors_for_accessibility,
)


class BrandGuardLoginView(LoginView):
    authentication_form = UsernameOrEmailAuthenticationForm
    template_name = "accounts/login.html"
    redirect_authenticated_user = True
    redirect_field_name = "next"


class PostOnlyLogoutView(LogoutView):
    http_method_names = ["post", "options"]
    next_page = "landing"


@require_http_methods(["GET", "POST"])
@workspace_member_required
def profile_settings(request):
    """Edit the signed-in user's names or securely change their password."""
    profile_form = AccountProfileForm(instance=request.user)
    password_form = AccountPasswordChangeForm(request.user)
    active_form = "profile"

    if request.method == "POST":
        action = request.POST.get("form_action")
        if action == "profile":
            profile_form = AccountProfileForm(request.POST, instance=request.user)
            if profile_form.is_valid():
                profile_form.save()
                messages.success(request, "Your account details have been updated.")
                return redirect("app:profile")
            mark_form_errors_for_accessibility(profile_form)
            active_form = "profile"
        elif action == "password":
            password_form = AccountPasswordChangeForm(request.user, request.POST)
            if password_form.is_valid():
                user = password_form.save()
                update_session_auth_hash(request, user)
                messages.success(request, "Your password has been changed.")
                return redirect("app:profile")
            mark_form_errors_for_accessibility(password_form)
            active_form = "password"
        else:
            return HttpResponseBadRequest("Choose a supported account settings form.")

    context = app_page_context(
        request,
        page_title="Account settings",
        active_navigation="profile",
        extra={
            "profile_form": profile_form,
            "password_form": password_form,
            "active_form": active_form,
        },
    )
    return render(request, "accounts/profile_settings.html", context)
