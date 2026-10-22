from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from brand_sources.services import policy_snapshot
from content_reviews.models import ContentSubmission
from workspaces.models import WorkspaceMembership
from workspaces.page_context import app_page_context
from workspaces.permissions import workspace_member_required, workspace_role_required

from .forms import ApproveVersionForm, RequestChangesForm
from .models import ApprovalRequest
from .services import REVIEWER_ROLES, approve_version, request_changes


def _context(request, title, extra=None):
    return app_page_context(
        request,
        page_title=title,
        active_navigation="approvals",
        extra=extra or {},
    )


@require_GET
@workspace_role_required(*REVIEWER_ROLES)
def approval_inbox(request):
    snapshot = policy_snapshot(request.workspace)
    pending = (
        ApprovalRequest.objects.filter(workspace=request.workspace, status=ApprovalRequest.Status.IN_REVIEW)
        .select_related("submission", "submission__creator", "content_version", "review_run")
        .prefetch_related("review_run__findings__rule")
        .order_by("requested_at", "id")
    )
    rows = []
    for approval in pending:
        findings = approval.review_run.findings.all()
        rows.append({
            "approval": approval,
            "high_count": sum(1 for item in findings if item.rule.severity == "HIGH"),
            "finding_count": len(findings),
            "stale": (
                approval.submission.current_version_id != approval.content_version_id
                or approval.policy_snapshot != snapshot
                or approval.content_hash != approval.content_version.content_hash
            ),
        })
    return render(request, "approvals/inbox.html", _context(request, "Approval inbox", {"rows": rows}))


@require_http_methods(["GET", "POST"])
@workspace_member_required
def approval_detail(request, request_id):
    approval = get_object_or_404(
        ApprovalRequest.objects.select_related(
            "workspace", "submission", "submission__creator", "content_version", "review_run", "requested_by"
        ).prefetch_related(
            "review_run__findings__rule__source_version__source",
            "review_run__findings__review_check",
        ),
        pk=request_id,
        workspace=request.workspace,
    )
    role = request.workspace_membership.role
    is_creator = role == WorkspaceMembership.Role.CREATOR
    if is_creator and approval.submission.creator_id != request.user.pk:
        raise Http404("This approval request is not available.")

    can_decide = (
        role in REVIEWER_ROLES
        and approval.requested_by_id != request.user.pk
        and approval.status == ApprovalRequest.Status.IN_REVIEW
    )
    current_snapshot = policy_snapshot(request.workspace)
    stale_reasons = []
    if approval.submission.current_version_id != approval.content_version_id:
        stale_reasons.append("A newer content version is current.")
    if approval.content_hash != approval.content_version.content_hash:
        stale_reasons.append("The reviewed content hash no longer matches the request.")
    if current_snapshot != approval.policy_snapshot:
        stale_reasons.append("The approved brand policy changed after this review.")
    if approval.review_run.status != "COMPLETE":
        stale_reasons.append("The linked review is not complete.")
    if approval.review_run.checks.filter(outcome__in=["FAILED", "NOT_COMPLETED"]).exists():
        stale_reasons.append("One or more policy checks did not complete.")
    stale = bool(stale_reasons)
    findings = list(
        approval.review_run.findings.select_related(
            "rule", "rule__source_version", "rule__source_version__source", "source_version__source"
        ).order_by("rule__severity", "created_at")
    )
    for finding in findings:
        finding.source_label = f"{finding.source_version.source.title}, v{finding.source_version.version_number}"

    changes_form = RequestChangesForm(request.POST if request.method == "POST" and "request_changes" in request.POST else None)
    approve_form = ApproveVersionForm(request.POST if request.method == "POST" and "approve_version" in request.POST else None)
    if request.method == "POST":
        if not can_decide:
            raise PermissionDenied("Your workspace role cannot make approval decisions.")
        if "request_changes" in request.POST and changes_form.is_valid():
            try:
                request_changes(
                    request_id=approval.pk, workspace=request.workspace, actor=request.user,
                    comment=changes_form.cleaned_data["comment"],
                )
            except (ApprovalRequest.DoesNotExist, ValidationError) as exc:
                if isinstance(exc, ApprovalRequest.DoesNotExist):
                    raise Http404("This approval request is not available.") from exc
                changes_form.add_error(None, " ".join(getattr(exc, "messages", [str(exc)])))
            else:
                messages.success(request, "Changes requested. The creator can now prepare a new content version.")
                return redirect("app:approvals:detail", request_id=approval.pk)
        elif "approve_version" in request.POST and approve_form.is_valid():
            try:
                approve_version(
                    request_id=approval.pk, workspace=request.workspace, actor=request.user,
                    override_reason=approve_form.cleaned_data["override_reason"],
                )
            except (ApprovalRequest.DoesNotExist, ValidationError) as exc:
                if isinstance(exc, ApprovalRequest.DoesNotExist):
                    raise Http404("This approval request is not available.") from exc
                approve_form.add_error(None, " ".join(getattr(exc, "messages", [str(exc)])))
            else:
                messages.success(request, "This exact content version is approved against the recorded policy snapshot.")
                return redirect("app:approvals:detail", request_id=approval.pk)

    return render(request, "approvals/detail.html", _context(request, f"Approval · {approval.submission.title}", {
        "approval": approval,
        "submission": approval.submission,
        "version": approval.content_version,
        "review": approval.review_run,
        "findings": findings,
        "has_high_findings": any(finding.rule.severity == "HIGH" for finding in findings),
        "can_decide": can_decide,
        "stale": stale,
        "stale_reasons": stale_reasons,
        "current_snapshot": current_snapshot,
        "changes_form": changes_form,
        "approve_form": approve_form,
        "decision": getattr(approval, "decision", None),
    }))
