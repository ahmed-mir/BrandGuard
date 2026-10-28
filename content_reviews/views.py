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

from .collaboration import add_review_comment, change_finding_resolution, resolution_choices
from .collaboration_forms import FindingResolutionForm, ReviewCommentForm
from .forms import ContentRevisionForm, ContentSubmissionForm, StartReviewForm, SubmitForApprovalForm
from .models import ContentSubmission, ContentVersion, FindingResolutionEvent, ReviewCheck, ReviewComment, ReviewFinding, ReviewRun
from .readiness import build_review_readiness
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


def _can_collaborate_on_submission(request, submission):
    return (
        request.workspace_membership.role != CREATOR
        or submission.creator_id == request.user.pk
    )


def _role_label(role):
    return dict(WorkspaceMembership.Role.choices).get(role, role)


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
    form = ContentSubmissionForm(request.POST if request.method == "POST" else None)
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
            submission_data = form.cleaned_data.copy()
            idempotency_key = submission_data.pop("idempotency_key")
            submission, version = create_submission(
                workspace=request.workspace,
                creator=request.user,
                idempotency_key=idempotency_key,
                **submission_data,
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
    readiness = build_review_readiness(
        workspace=request.workspace,
        membership=request.workspace_membership,
        submission=submission,
        version=version,
        review_runs=review_runs,
    )
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
    can_comment = _can_collaborate_on_submission(request, submission)
    version_comments = list(
        ReviewComment.objects.filter(
            workspace_id=request.workspace.pk,
            submission=submission,
            content_version=version,
            finding__isnull=True,
        ).select_related("author").order_by("created_at", "id")
    )
    for comment in version_comments:
        comment.author_role_label = _role_label(comment.author_role)
    submit_form = None
    fresh_review = readiness["review"]["run"] if readiness["review"]["fresh"] else None
    if creator_view and submission.creator_id == request.user.pk and submission.current_version_id == version.pk:
        prior_approval_is_for_another_version = (
            readiness["approval"]["key"] == "STALE"
            and readiness["approval"]["request"] is not None
            and readiness["approval"]["request"].content_version_id != version.pk
        )
        can_request_current_approval = readiness["approval"]["key"] == "NOT_REQUESTED" or prior_approval_is_for_another_version
        if fresh_review and can_request_current_approval:
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
            "review_readiness": readiness,
            "submit_form": submit_form,
            "fresh_review": fresh_review,
            "can_revise": creator_view and submission.creator_id == request.user.pk and submission.current_version_id == version.pk,
            "ai_analysis_runs": ai_analysis_runs,
            "ai_analysis_available": ai_analysis_available(),
            "ai_analysis_demo_mode": ai_analysis_mode() == "demo",
            "ai_unavailable_reason": ai_unavailable_reason(),
            "can_request_ai_analysis": creator_view and submission.creator_id == request.user.pk,
            "ai_analysis_consent_form": AIAnalysisConsentForm(demo_mode=ai_analysis_mode() == "demo"),
            "can_comment": can_comment,
            "version_comments": version_comments,
            "version_comment_form": ReviewCommentForm(prefix="version-comment"),
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
    if submission.current_version_id is None:
        raise Http404("This submission has no current content version.")
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
    # Diff inputs are bounded to 20,000 characters. Keep difflib's autojunk
    # heuristic enabled so repetitive text cannot trigger quadratic matching.
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, old_lines, new_lines).get_opcodes():
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
    can_comment = _can_collaborate_on_submission(request, submission)
    finding_ids = [finding.pk for finding in findings]
    comments_by_finding = {}
    for comment in ReviewComment.objects.filter(
        workspace_id=request.workspace.pk,
        submission=submission,
        content_version=version,
        finding_id__in=finding_ids,
    ).select_related("author").order_by("created_at", "id"):
        comment.author_role_label = _role_label(comment.author_role)
        comments_by_finding.setdefault(comment.finding_id, []).append(comment)
    events_by_finding = {}
    for event in FindingResolutionEvent.objects.filter(
        workspace_id=request.workspace.pk,
        content_version=version,
        finding_id__in=finding_ids,
    ).select_related("actor").order_by("created_at", "id"):
        event.actor_role_label = _role_label(event.actor_role)
        events_by_finding.setdefault(event.finding_id, []).append(event)
    for finding in findings:
        finding.collaboration_comments = comments_by_finding.get(finding.pk, [])
        finding.resolution_history = events_by_finding.get(finding.pk, [])
        choices = resolution_choices(
            membership=request.workspace_membership, finding=finding, submission=submission
        ) if can_comment else []
        finding.resolution_choices = choices
        if choices:
            finding.resolution_form = FindingResolutionForm(
                current_status=finding.resolution_status,
                choices=choices,
                prefix=f"finding-{finding.pk}-resolution",
            )
        finding.comment_form = ReviewCommentForm(prefix=f"finding-{finding.pk}-comment") if can_comment else None
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
            "can_comment": can_comment,
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


@require_POST
@workspace_member_required
def post_version_comment(request, submission_id, version_number):
    submission = _submission(request, submission_id)
    form = ReviewCommentForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Check the comment and try again.")
        return redirect("app:content_reviews:version_detail", submission_id=submission.pk, version_number=version_number)
    try:
        comment, created = add_review_comment(
            workspace=request.workspace,
            actor=request.user,
            submission_id=submission.pk,
            version_number=version_number,
            body=form.cleaned_data["body"],
            idempotency_key=form.cleaned_data["idempotency_key"],
        )
    except ContentSubmission.DoesNotExist as exc:
        raise Http404("This submission is not available.") from exc
    except ContentVersion.DoesNotExist as exc:
        raise Http404("This content version is not available.") from exc
    if created:
        messages.success(request, "Comment added to the version discussion.")
    else:
        messages.info(request, "This comment was already saved.")
    return redirect("app:content_reviews:version_detail", submission_id=submission.pk, version_number=version_number)


@require_POST
@workspace_member_required
def post_finding_comment(request, submission_id, version_number, finding_id):
    submission = _submission(request, submission_id)
    form = ReviewCommentForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Check the comment and try again.")
        review_id = _finding_review_id(request, submission, version_number, finding_id)
        return redirect("app:content_reviews:review_detail", submission_id=submission.pk, version_number=version_number, review_id=review_id)
    try:
        comment, created = add_review_comment(
            workspace=request.workspace,
            actor=request.user,
            submission_id=submission.pk,
            version_number=version_number,
            finding_id=finding_id,
            body=form.cleaned_data["body"],
            idempotency_key=form.cleaned_data["idempotency_key"],
        )
    except (ContentVersion.DoesNotExist, ReviewFinding.DoesNotExist) as exc:
        raise Http404("This finding is not available for the selected version.") from exc
    except ContentSubmission.DoesNotExist as exc:
        raise Http404("This submission is not available.") from exc
    if created:
        messages.success(request, "Comment added to the finding discussion.")
    else:
        messages.info(request, "This comment was already saved.")
    review_id = comment.finding.review_run_id if comment.finding_id else _finding_review_id(request, submission, version_number, finding_id)
    return redirect("app:content_reviews:review_detail", submission_id=submission.pk, version_number=version_number, review_id=review_id)


@require_POST
@workspace_member_required
def update_finding_resolution(request, submission_id, version_number, finding_id):
    submission = _submission(request, submission_id)
    form = FindingResolutionForm(
        request.POST,
        current_status=ReviewFinding.ResolutionStatus.OPEN,
        choices=ReviewFinding.ResolutionStatus.choices,
    )
    if not form.is_valid():
        messages.error(request, "Check the finding status and try again.")
        review_id = _finding_review_id(request, submission, version_number, finding_id)
        return redirect("app:content_reviews:review_detail", submission_id=submission.pk, version_number=version_number, review_id=review_id)
    try:
        event, changed = change_finding_resolution(
            workspace=request.workspace,
            actor=request.user,
            submission_id=submission.pk,
            version_number=version_number,
            finding_id=finding_id,
            expected_status=form.cleaned_data["expected_status"],
            new_status=form.cleaned_data["new_status"],
            note=form.cleaned_data["note"],
            idempotency_key=form.cleaned_data["idempotency_key"],
        )
    except (ContentSubmission.DoesNotExist, ContentVersion.DoesNotExist, ReviewFinding.DoesNotExist) as exc:
        raise Http404("This finding is not available for the selected version.") from exc
    except ValidationError as exc:
        messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))
        review_id = _finding_review_id(request, submission, version_number, finding_id)
        return redirect("app:content_reviews:review_detail", submission_id=submission.pk, version_number=version_number, review_id=review_id)
    if changed:
        messages.success(request, f"Finding status recorded as {event.get_new_status_display()}. The original review finding remains unchanged.")
    else:
        messages.info(request, "The finding already has that status; no duplicate history entry was created.")
    review_id = event.finding.review_run_id if event else _finding_review_id(request, submission, version_number, finding_id)
    return redirect("app:content_reviews:review_detail", submission_id=submission.pk, version_number=version_number, review_id=review_id)


def _finding_review_id(request, submission, version_number, finding_id):
    finding = get_object_or_404(
        ReviewFinding.objects.filter(
            review_run__workspace_id=request.workspace.pk,
            review_run__content_version__submission=submission,
            review_run__content_version__version_number=version_number,
        ),
        pk=finding_id,
    )
    return finding.review_run_id


