from difflib import SequenceMatcher
import uuid
import logging

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from brand_sources.models import BrandRule
from workspaces.models import WorkspaceMembership
from workspaces.page_context import app_page_context
from workspaces.permissions import workspace_member_required, workspace_role_required
from brand_sources.services import policy_snapshot

from .forms import ContentRevisionForm, ContentSubmissionForm, StartReviewForm, SubmitForApprovalForm
from .models import ContentSubmission, ContentVersion, ReviewCheck, ReviewFinding, ReviewRun
from .services import create_revision, create_submission, execute_review


CREATOR = WorkspaceMembership.Role.CREATOR
logger = logging.getLogger(__name__)


def _context(request, title, extra=None):
    return app_page_context(
        request,
        page_title=title,
        active_navigation="reviews",
        extra={
            "can_submit_content": request.workspace_membership.role == CREATOR,
            **(extra or {}),
        },
    )


def _submissions(request):
    qs = ContentSubmission.objects.filter(workspace_id=request.workspace.pk).select_related(
        "creator", "current_version"
    )
    if request.workspace_membership.role == CREATOR:
        qs = qs.filter(creator_id=request.user.pk)
    return qs


def _submission(request, submission_id):
    submission = get_object_or_404(_submissions(request), pk=submission_id)
    return submission


@require_GET
@workspace_member_required
def submission_list(request):
    submissions = _submissions(request)
    from approvals.services import submission_workflow_status
    for submission in submissions:
        status = submission_workflow_status(submission)
        submission.workflow_state = status["state"]
        submission.workflow_label = status["label"]
        submission.approval_is_stale = status["stale"]
    return render(
        request,
        "content_reviews/submission_list.html",
        _context(request, "Content reviews", {"submissions": submissions}),
    )


@require_http_methods(["GET", "POST"])
@workspace_role_required(WorkspaceMembership.Role.CREATOR)
def submission_create(request):
    form = ContentSubmissionForm(request.POST or None)
    demo_example = None
    if request.workspace.is_demo:
        demo_example = {
            "title": "LumaDesk internal pilot announcement",
            "channel": ContentSubmission.Channel.EMAIL,
            "audience": "Prospective team leads",
            "campaign_purpose": "Introduce the fictional internal pilot",
            "content_text": (
                "Up to 20% faster task completion in our internal pilot; results vary. "
                "LumaDesk is a productivity miracle. We guarantee 100% faster task "
                "completion for every team."
            ),
        }
    if request.method == "POST" and form.is_valid():
        try:
            submission, version = create_submission(
                workspace=request.workspace,
                creator=request.user,
                **form.cleaned_data,
            )
        except ValidationError as exc:
            if hasattr(exc, "error_dict"):
                for field, errors in exc.error_dict.items():
                    for error in errors:
                        form.add_error(field if field in form.fields else None, error)
            else:
                form.add_error(None, exc.messages)
        else:
            messages.success(request, "Content saved as immutable version 1. Start a review when you are ready.")
            return redirect("app:content_reviews:version_detail", submission_id=submission.pk, version_number=version.version_number)
    return render(request, "content_reviews/submission_form.html", _context(request, "New content submission", {
        "form": form,
        "demo_example": demo_example,
    }))


@require_GET
@workspace_member_required
def submission_detail(request, submission_id):
    submission = _submission(request, submission_id)
    if submission.current_version_id:
        return redirect(
            "app:content_reviews:version_detail",
            submission_id=submission.pk,
            version_number=submission.current_version.version_number,
        )
    raise Http404("This submission has no saved content version.")


@require_GET
@workspace_member_required
def version_detail(request, submission_id, version_number):
    submission = _submission(request, submission_id)
    version = get_object_or_404(submission.versions, version_number=version_number)
    review_runs = version.review_runs.select_related("requested_by").all()
    latest_review = review_runs.first()
    from approvals.services import current_valid_approval
    from ai_analysis.forms import AIAnalysisConsentForm
    from ai_analysis.models import AIAnalysisRun
    from ai_analysis.services import ai_analysis_available, ai_analysis_mode, ai_unavailable_reason, current_analysis_is_stale
    current_approval = current_valid_approval(submission)
    current_policy = policy_snapshot(request.workspace)
    version_rows = []
    for historical_version in submission.versions.select_related("created_by").prefetch_related("approval_requests__decision").order_by("-version_number"):
        approval = historical_version.approval_requests.select_related("decision").order_by("-requested_at").first()
        latest_version_review = historical_version.review_runs.first()
        history_label = approval.get_status_display() if approval else "Draft"
        if approval and approval.status == "APPROVED" and historical_version.pk != submission.current_version_id:
            history_label = "Approved · historical"
        elif (
            approval
            and approval.status == "APPROVED"
            and current_approval["approval"]
            and approval.pk == current_approval["approval"].pk
            and not current_approval["valid"]
        ):
            history_label = "Approval no longer current"
        version_rows.append({
            "version": historical_version,
            "approval": approval,
            "workflow_status": history_label,
            "review_status": latest_version_review.get_status_display() if latest_version_review else "Not reviewed",
            "decision": getattr(approval, "decision", None) if approval else None,
        })
    creator_view = request.workspace_membership.role == CREATOR
    approval_history = submission.approval_requests.select_related("content_version", "requested_by").prefetch_related("decision__reviewer").order_by("-requested_at")
    ai_analysis_runs = list(
        AIAnalysisRun.objects.filter(workspace_id=request.workspace.pk, content_version=version)
        .select_related("requested_by", "content_version")
        .prefetch_related("findings__source_version__source")[:20]
    )
    for ai_run in ai_analysis_runs:
        ai_run.is_stale = current_analysis_is_stale(ai_run, submission, current_policy)
    submit_form = None
    fresh_review = None
    if creator_view and submission.creator_id == request.user.pk and submission.current_version_id == version.pk:
        for candidate in review_runs:
            if candidate.status == ReviewRun.Status.COMPLETE and candidate.policy_snapshot == current_policy and candidate.checks.exists():
                fresh_review = candidate
                break
        if fresh_review:
            submit_form = SubmitForApprovalForm(initial={"review_run_id": fresh_review.pk}, content_version=version)
    return render(
        request,
        "content_reviews/version_detail.html",
        _context(request, f"{submission.title} · v{version.version_number}", {
            "submission": submission,
            "version": version,
            "review_runs": review_runs,
            "latest_review": latest_review,
            "start_review_form": StartReviewForm(),
            "version_rows": version_rows,
            "approval_history": approval_history,
            "current_approval": current_approval,
            "submit_form": submit_form,
            "fresh_review": fresh_review,
            "can_revise": creator_view and submission.creator_id == request.user.pk and submission.current_version_id == version.pk,
            "ai_analysis_runs": ai_analysis_runs,
            "ai_analysis_available": ai_analysis_available(),
            "ai_analysis_demo_mode": ai_analysis_mode() == "demo",
            "ai_unavailable_reason": ai_unavailable_reason(),
            "can_request_ai_analysis": creator_view and submission.creator_id == request.user.pk,
            "ai_analysis_consent_form": AIAnalysisConsentForm(demo_mode=ai_analysis_mode() == "demo"),
        }),
    )


@require_http_methods(["GET", "POST"])
@workspace_role_required(WorkspaceMembership.Role.CREATOR)
def revise_submission(request, submission_id):
    submission = _submission(request, submission_id)
    if submission.creator_id != request.user.pk or submission.current_version_id is None:
        raise Http404("This submission is not available for revision.")
    base = submission.current_version
    initial = {
        "content_text": base.content_text,
        "base_version_id": base.pk,
        "base_content_hash": base.content_hash,
        "idempotency_key": uuid.uuid4(),
    }
    form = ContentRevisionForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            updated_submission, version, created = create_revision(
                submission_id=submission.pk,
                workspace=request.workspace,
                actor=request.user,
                base_version_id=form.cleaned_data["base_version_id"],
                base_content_hash=form.cleaned_data["base_content_hash"],
                content_text=form.cleaned_data["content_text"],
                idempotency_key=form.cleaned_data["idempotency_key"],
            )
        except ContentSubmission.DoesNotExist as exc:
            raise Http404("This submission is not available for revision.") from exc
        except ValidationError as exc:
            if hasattr(exc, "error_dict"):
                for field, errors in exc.error_dict.items():
                    for error in errors:
                        form.add_error(field if field in form.fields else None, error)
            else:
                form.add_error(None, " ".join(exc.messages))
        except Exception as exc:
            logger.error(
                "Unexpected revision save failure (workspace_id=%s, submission_id=%s, error_type=%s).",
                request.workspace.pk,
                submission.pk,
                type(exc).__name__,
                exc_info=True,
            )
            raise
        else:
            if created:
                messages.success(request, f"Saved as new immutable content version {version.version_number}. Run a fresh review before submitting it for approval.")
            else:
                messages.info(request, f"This revision was already saved as version {version.version_number}.")
            return redirect("app:content_reviews:version_detail", submission_id=updated_submission.pk, version_number=version.version_number)
    return render(request, "content_reviews/revision_form.html", _context(request, f"Revise · {submission.title}", {
        "submission": submission,
        "base_version": base,
        "form": form,
    }))


@require_POST
@workspace_role_required(WorkspaceMembership.Role.CREATOR)
def submit_submission(request, submission_id):
    from approvals.services import submit_for_approval

    submission = _submission(request, submission_id)
    form = SubmitForApprovalForm(request.POST, content_version=submission.current_version)
    if not form.is_valid():
        messages.error(request, "Refresh the current version and choose its latest completed review before submitting.")
        return redirect("app:content_reviews:version_detail", submission_id=submission.pk, version_number=submission.current_version.version_number)
    try:
        approval, created = submit_for_approval(
            submission_id=submission.pk,
            workspace=request.workspace,
            actor=request.user,
            review_run_id=form.cleaned_data["review_run_id"],
        )
    except ContentSubmission.DoesNotExist as exc:
        raise Http404("This submission is not available.") from exc
    except ValidationError as exc:
        messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))
        return redirect("app:content_reviews:version_detail", submission_id=submission.pk, version_number=submission.current_version.version_number)
    messages.success(request, "Submitted for human approval." if created else "This version is already awaiting a decision.")
    return redirect("app:approvals:detail", request_id=approval.pk)


@require_GET
@workspace_member_required
def compare_versions(request, submission_id):
    submission = _submission(request, submission_id)
    try:
        old_number = int(request.GET.get("from", ""))
        new_number = int(request.GET.get("to", ""))
    except (TypeError, ValueError):
        raise Http404("Choose two saved content versions to compare.")
    if old_number == new_number:
        raise Http404("Choose two different versions to compare.")
    older = get_object_or_404(submission.versions, version_number=old_number)
    newer = get_object_or_404(submission.versions, version_number=new_number)
    rows = []
    old_lines = older.content_text.splitlines(keepends=True)
    new_lines = newer.content_text.splitlines(keepends=True)
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes():
        if tag == "equal":
            rows.extend({"kind": "equal", "old": old_lines[i], "new": new_lines[j]} for i, j in zip(range(i1, i2), range(j1, j2)))
        elif tag == "delete":
            rows.extend({"kind": "removed", "old": line, "new": ""} for line in old_lines[i1:i2])
        elif tag == "insert":
            rows.extend({"kind": "added", "old": "", "new": line} for line in new_lines[j1:j2])
        else:
            width = max(i2 - i1, j2 - j1)
            for offset in range(width):
                old_line = old_lines[i1 + offset] if i1 + offset < i2 else ""
                new_line = new_lines[j1 + offset] if j1 + offset < j2 else ""
                rows.append({"kind": "changed", "old": old_line, "new": new_line})
    return render(request, "content_reviews/version_compare.html", _context(request, f"Compare versions · {submission.title}", {
        "submission": submission, "older": older, "newer": newer, "diff_rows": rows,
    }))


@require_GET
@workspace_member_required
def review_detail(request, submission_id, version_number, review_id):
    submission = _submission(request, submission_id)
    version = get_object_or_404(submission.versions, version_number=version_number)
    review = get_object_or_404(
        ReviewRun.objects.select_related("workspace", "requested_by", "content_version"),
        pk=review_id,
        content_version=version,
        workspace_id=request.workspace.pk,
    )
    checks = review.checks.select_related("rule", "source_version", "source_version__source").prefetch_related("findings").all()
    findings = list(
        review.findings.select_related(
            "rule", "rule__source_version", "rule__source_version__source", "source_version__source"
        ).all()
    )
    findings_by_severity = [
        (label, [finding for finding in findings if finding.rule.severity == value])
        for value, label in BrandRule.Severity.choices
    ]
    content = version.content_text
    for finding in findings:
        finding.context_before = ""
        finding.context_after = ""
        finding.highlight_text = ""
        if (
            finding.location_status == ReviewFinding.LocationStatus.AVAILABLE
            and finding.text_start is not None
            and finding.text_end is not None
            and content[finding.text_start:finding.text_end] == finding.matched_text
        ):
            start = finding.text_start
            end = finding.text_end
            context_start = max(0, start - 90)
            context_end = min(len(content), end + 90)
            finding.context_before = ("…" if context_start else "") + content[context_start:start]
            finding.highlight_text = content[start:end]
            finding.context_after = content[end:context_end] + ("…" if context_end < len(content) else "")
        else:
            finding.location_status = ReviewFinding.LocationStatus.UNAVAILABLE
    current_policy = policy_snapshot(request.workspace)
    policy_changed = (
        current_policy["revision"] != review.policy_revision
        or current_policy["hash"] != review.policy_snapshot_hash
    )
    return render(
        request,
        "content_reviews/review_detail.html",
        _context(request, f"Review · {submission.title}", {
            "submission": submission,
            "version": version,
            "review": review,
            "checks": checks,
            "findings": findings,
            "findings_by_severity": findings_by_severity,
            "policy_versions": review.pinned_policy_versions.select_related("source_version", "source_version__source").all(),
            "policy_changed": policy_changed,
            "current_policy": current_policy,
            "no_policy_rules": not review.checks.exists() and review.status == ReviewRun.Status.COMPLETE,
            "only_undetermined_checks": review.checks.exists() and not review.checks.exclude(
                outcome__in=[ReviewCheck.Outcome.NOT_APPLICABLE, ReviewCheck.Outcome.NEEDS_HUMAN_REVIEW]
            ).exists(),
            "human_review_checks": review.checks.filter(outcome=ReviewCheck.Outcome.NEEDS_HUMAN_REVIEW).count(),
        }),
    )


@require_POST
@workspace_role_required(WorkspaceMembership.Role.CREATOR)
def review_start(request, submission_id, version_number):
    submission = _submission(request, submission_id)
    version = get_object_or_404(submission.versions, version_number=version_number)
    form = StartReviewForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Refresh the submission page before starting another review.")
        return redirect("app:content_reviews:version_detail", submission_id=submission.pk, version_number=version.version_number)
    try:
        review = execute_review(
            submission_id=submission.pk,
            version_number=version.version_number,
            workspace=request.workspace,
            actor=request.user,
            idempotency_key=form.cleaned_data["idempotency_key"],
        )
    except ContentSubmission.DoesNotExist:
        raise Http404("This submission is not available in the current workspace.")
    except ValidationError as exc:
        messages.error(request, "The review could not be started: " + " ".join(getattr(exc, "messages", [str(exc)])))
        return redirect("app:content_reviews:version_detail", submission_id=submission.pk, version_number=version.version_number)
    return redirect(
        "app:content_reviews:review_detail",
        submission_id=submission.pk,
        version_number=version.version_number,
        review_id=review.pk,
    )


