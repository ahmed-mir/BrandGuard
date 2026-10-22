from django.shortcuts import render
from django.views.decorators.http import require_GET

from .models import WorkspaceMembership
from .page_context import app_page_context
from .permissions import workspace_member_required, workspace_role_required


ROLE_DESCRIPTIONS = {
    WorkspaceMembership.Role.CREATOR: (
        "Submit campaign text, check it against approved brand rules, and inspect the exact source evidence behind deterministic findings."
    ),
    WorkspaceMembership.Role.BRAND_MANAGER: (
        "Maintain approved, evidence-backed guidelines and inspect workspace submissions and their pinned policy review history."
    ),
    WorkspaceMembership.Role.REVIEWER: (
        "Inspect submitted content and its deterministic, source-backed review results. Content approval decisions are not part of this phase."
    ),
    WorkspaceMembership.Role.ADMIN: (
        "Oversee workspace access and inspect workspace submissions and their source-backed policy review history."
    ),
}


@require_GET
@workspace_member_required
def home(request):
    membership = request.workspace_membership
    context = app_page_context(
        request,
        page_title="Workspace overview",
        active_navigation="overview",
        extra={"role_description": ROLE_DESCRIPTIONS[membership.role]},
    )
    return render(request, "core/app_home.html", context)


@require_GET
@workspace_role_required(WorkspaceMembership.Role.ADMIN)
def members(request):
    membership = request.workspace_membership
    member_records = (
        WorkspaceMembership.objects.filter(workspace_id=membership.workspace_id)
        .select_related("user", "workspace")
        .order_by("user__first_name", "user__last_name", "user__username")
    )
    context = app_page_context(
        request,
        page_title="Workspace members",
        active_navigation="members",
        extra={"member_records": member_records},
    )
    return render(request, "workspaces/members.html", context)
