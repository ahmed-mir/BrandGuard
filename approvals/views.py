from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Case, CharField, Count, Q, Value, When
from django.db.models.functions import Concat
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
from .services import REVIEWER_ROLES, approve_version, current_valid_approval, request_changes


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
    status_options = [
        ("pending", "Pending", ApprovalRequest.Status.IN_REVIEW),
        ("changes_requested", "Changes requested", ApprovalRequest.Status.CHANGES_REQUESTED),
        ("approved", "Approved", ApprovalRequest.Status.APPROVED),
        ("all", "All", None),
    ]
    status_filter = request.GET.get("status", "pending")
    valid_statuses = {key for key, _label, _value in status_options}
    if status_filter not in valid_statuses:
        status_filter = "pending"
    query = str(request.GET.get("q", ""))[:120].strip()

    requests = (
        ApprovalRequest.objects.filter(workspace=request.workspace)
        .select_related("submission", "submission__creator", "content_version", "review_run")
        .annotate(
            finding_count=Count("review_run__findings", distinct=True),
            high_count=Count(
                "review_run__findings",
                filter=Q(review_run__findings__rule__severity="HIGH"),
                distinct=True,
            ),
        )
    )
    selected_status = next(value for key, _label, value in status_options if key == status_filter)
    if selected_status:
        requests = requests.filter(status=selected_status)
    if query:
        requests = requests.annotate(
            _creator_display_name=Concat(
                "submission__creator__first_name",
                Value(" "),
                "submission__creator__last_name",
                output_field=CharField(),
            )
        ).filter(
            Q(submission__title__icontains=query)
            | Q(submission__creator__username__icontains=query)
            | Q(_creator_display_name__icontains=query)
        )

    if status_filter == "all":
        requests = requests.order_by(
            Case(
                When(status=ApprovalRequest.Status.IN_REVIEW, then=Value(0)),
                When(status=ApprovalRequest.Status.CHANGES_REQUESTED, then=Value(1)),
                default=Value(2),
            ),
            "-requested_at", "-id",
        )
    else:
        requests = requests.order_by("-requested_at", "-id")

    rows = [{
        "approval": approval,
        "high_count": approval.high_count,
        "finding_count": approval.finding_count,
        "stale": (
            approval.submission.current_version_id != approval.content_version_id
            or approval.policy_snapshot != snapshot
            or approval.content_hash != approval.content_version.content_hash
        ),
    } for approval in requests]
    return render(request, "approvals/inbox.html", _context(request, "Approval inbox", {
        "rows": rows,
        "status_filter": status_filter,
        "status_options": [(key, label) for key, label, _value in status_options],
        "query": query,
        "has_filters": bool(query) or status_filter != "pending",
        "result_count": len(rows),
    }))


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

    current_approval = current_valid_approval(approval.submission)
    can_export = (
        current_approval["valid"]
        and current_approval["approval"].pk == approval.pk
        and approval.content_version_id == approval.submission.current_version_id
    )
    if approval.status == ApprovalRequest.Status.APPROVED and not can_export:
        stale_reasons.append(current_approval["reason"])
        stale = True

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
        "current_approval": current_approval,
        "can_export": can_export,
        "changes_form": changes_form,
        "approve_form": approve_form,
        "decision": getattr(approval, "decision", None),
    }))
