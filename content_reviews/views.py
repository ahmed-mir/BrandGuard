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

from .forms import ContentSubmissionForm, StartReviewForm
from .models import ContentSubmission, ContentVersion, ReviewCheck, ReviewFinding, ReviewRun
from .services import create_submission, execute_review


CREATOR = WorkspaceMembership.Role.CREATOR


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
    return render(
        request,
        "content_reviews/submission_list.html",
        _context(request, "Content reviews", {"submissions": submissions}),
    )


@require_http_methods(["GET", "POST"])
@workspace_role_required(WorkspaceMembership.Role.CREATOR)
def submission_create(request):
    form = ContentSubmissionForm(request.POST or None)
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
    return render(request, "content_reviews/submission_form.html", _context(request, "New content submission", {"form": form}))


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
    return render(
        request,
        "content_reviews/version_detail.html",
        _context(request, f"{submission.title} · v{version.version_number}", {
            "submission": submission,
            "version": version,
            "review_runs": review_runs,
            "latest_review": latest_review,
            "start_review_form": StartReviewForm(),
        }),
    )


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


