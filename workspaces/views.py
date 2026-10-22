from django.shortcuts import render
from django.views.decorators.http import require_GET

from .models import WorkspaceMembership
from .page_context import app_page_context
from .permissions import workspace_member_required, workspace_role_required


ROLE_DESCRIPTIONS = {
    WorkspaceMembership.Role.CREATOR: (
        "Use this workspace to submit campaign content and work through evidence-backed "
        "feedback when content review becomes available."
    ),
    WorkspaceMembership.Role.BRAND_MANAGER: (
        "Maintain approved brand guidelines and product facts that future reviews can cite. "
        "Brand source management is not available yet."
    ),
    WorkspaceMembership.Role.REVIEWER: (
        "Review submitted versions and record human decisions when review tools become "
        "available."
    ),
    WorkspaceMembership.Role.ADMIN: (
        "Oversee workspace access and configuration. You can inspect current memberships; "
        "membership changes are not available yet."
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
