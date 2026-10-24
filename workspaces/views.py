from django.shortcuts import render
from django.views.decorators.http import require_GET

from approvals.models import ApprovalRequest
from approvals.services import submission_workflow_status
from brand_sources.models import SourceVersion
from content_reviews.models import ContentSubmission, ReviewRun
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
        "Inspect submitted content, trace deterministic findings to approved evidence, and record a decision on the exact version submitted for review."
    ),
    WorkspaceMembership.Role.ADMIN: (
        "Oversee workspace access and inspect workspace submissions and their source-backed policy review history."
    ),
}


@require_GET
@workspace_member_required
def home(request):
    membership = request.workspace_membership
    workspace = request.workspace
    submissions = ContentSubmission.objects.filter(workspace_id=workspace.pk).select_related(
        "creator", "current_version"
    )
    review_runs = ReviewRun.objects.filter(workspace_id=workspace.pk).select_related(
        "content_version__submission", "requested_by"
    )
    approval_requests = ApprovalRequest.objects.filter(
        workspace_id=workspace.pk, status=ApprovalRequest.Status.IN_REVIEW
    )
    if membership.role == WorkspaceMembership.Role.CREATOR:
        submissions = submissions.filter(creator_id=request.user.pk)
        review_runs = review_runs.filter(content_version__submission__creator_id=request.user.pk)
        approval_requests = approval_requests.filter(requested_by_id=request.user.pk)

    recent_submissions = list(submissions.order_by("-updated_at", "-created_at")[:5])
    for submission in recent_submissions:
        workflow = submission_workflow_status(submission)
        submission.workflow_label = workflow["label"]
        submission.workflow_state = workflow["state"]
        submission.approval_is_stale = workflow["stale"]

    recent_reviews = review_runs.order_by("-started_at", "-id")[:5]
    can_open_inbox = membership.role in {
        WorkspaceMembership.Role.REVIEWER,
        WorkspaceMembership.Role.ADMIN,
    }
    approved_source_count = SourceVersion.objects.filter(
        source__workspace_id=workspace.pk,
        status=SourceVersion.Status.APPROVED,
    ).count()
    context = app_page_context(
        request,
        page_title="Workspace overview",
        active_navigation="overview",
        extra={
            "role_description": ROLE_DESCRIPTIONS[membership.role],
            "approved_source_count": approved_source_count,
            "submission_count": submissions.count(),
            "awaiting_review_count": approval_requests.count(),
            "recent_submissions": recent_submissions,
            "recent_reviews": recent_reviews,
            "can_open_approval_inbox": can_open_inbox,
        },
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
