"""Version- and policy-bound human approval workflow services."""
import hashlib

from django.core.exceptions import ValidationError
from functools import wraps

from django.db import IntegrityError, OperationalError, connection, transaction

from brand_sources.services import policy_snapshot
from content_reviews.models import ContentSubmission, ReviewCheck, ReviewRun
from workspaces.models import Workspace, WorkspaceMembership

from .models import ApprovalDecision, ApprovalRequest

REVIEWER_ROLES = (WorkspaceMembership.Role.REVIEWER, WorkspaceMembership.Role.ADMIN)
INCOMPLETE_OUTCOMES = (ReviewCheck.Outcome.FAILED, ReviewCheck.Outcome.NOT_COMPLETED)


def _translate_sqlite_contention(view):
    """Turn SQLite's row-lock substitute failure into a safe retry response."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except OperationalError as exc:
            if connection.vendor == "sqlite" and "locked" in str(exc).casefold():
                raise ValidationError("Another workspace action is being saved. Refresh and try again.") from exc
            raise
    return wrapped


def _has_role(workspace, actor, roles):
    return WorkspaceMembership.objects.filter(
        workspace=workspace, workspace__is_active=True, user=actor,
        user__is_active=True, is_active=True, role__in=roles,
    ).exists()


def review_is_complete(review):
    """Return whether a run satisfies the approval workflow's completion rule."""
    checks = review.checks.all()
    return (
        review.status == ReviewRun.Status.COMPLETE
        and checks.exists()
        and not checks.filter(outcome__in=INCOMPLETE_OUTCOMES).exists()
    )


# Retain the internal name for existing service call sites while exposing the
# exact same rule to read-only workflow guidance.
_review_is_complete = review_is_complete


@_translate_sqlite_contention
@transaction.atomic
def submit_for_approval(*, submission_id, workspace, actor, review_run_id):
    """Create one request for the current version after a fresh complete review."""
    locked_workspace = Workspace.objects.select_for_update().get(pk=workspace.pk, is_active=True)
    if not _has_role(locked_workspace, actor, (WorkspaceMembership.Role.CREATOR,)):
        raise ValidationError("Only an active creator in this workspace can submit content for approval.")
    submission = _locked_submission_for_approval(
        submission_id=submission_id,
        workspace_id=locked_workspace.pk,
        creator_id=actor.pk,
    ).first()
    if submission is None or submission.current_version_id is None:
        raise ContentSubmission.DoesNotExist
    version = submission.current_version
    review = ReviewRun.objects.filter(pk=review_run_id, workspace=locked_workspace, content_version=version).first()
    if review is None:
        raise ValidationError("Choose a review run for the current content version.")
    if not _review_is_complete(review):
        raise ValidationError("A complete review with saved policy checks is required. Rerun the review if it was partial or failed.")
    current = policy_snapshot(locked_workspace)
    if current != review.policy_snapshot:
        raise ValidationError("Approved brand policy changed after this review. Run a fresh review before submitting.")

    existing = ApprovalRequest.objects.filter(content_version=version).first()
    if existing:
        if existing.status == ApprovalRequest.Status.IN_REVIEW and existing.review_run_id == review.pk and existing.requested_by_id == actor.pk:
            return existing, False
        raise ValidationError("This content version already has an approval request. Create a new version before submitting again.")
    try:
        approval = ApprovalRequest.objects.create(
            workspace=locked_workspace, submission=submission, content_version=version,
            review_run=review, requested_by=actor, status=ApprovalRequest.Status.IN_REVIEW,
            content_hash=version.content_hash, policy_revision=review.policy_revision,
            policy_snapshot_hash=review.policy_snapshot_hash, policy_snapshot=review.policy_snapshot,
        )
    except IntegrityError as exc:
        raise ValidationError("An approval request for this version was just created. Refresh to see it.") from exc
    return approval, True


@_translate_sqlite_contention
@transaction.atomic
def request_changes(*, request_id, workspace, actor, comment):
    approval = _locked_request(request_id=request_id, workspace=workspace, actor=actor)
    _ensure_pending(approval, actor)
    _validate_fresh_review(approval, approval.workspace)
    comment = (comment or "").strip()
    if len(comment) < 10 or len(comment) > 3000:
        raise ValidationError({"comment": "Enter a clear request of 10 to 3,000 characters."})
    try:
        decision = _record_decision(approval, actor, ApprovalDecision.Action.CHANGES_REQUESTED, comment=comment)
        approval.status = ApprovalRequest.Status.CHANGES_REQUESTED
        approval.save(update_fields=["status"])
        return decision
    except IntegrityError as exc:
        raise ValidationError("A reviewer decision was recorded while you were viewing this request. Reload the page.") from exc


@_translate_sqlite_contention
@transaction.atomic
def approve_version(*, request_id, workspace, actor, override_reason=""):
    approval = _locked_request(request_id=request_id, workspace=workspace, actor=actor)
    _ensure_pending(approval, actor)
    _validate_fresh_review(approval, approval.workspace)
    # Operational collaboration states never clear a deterministic finding for
    # approval purposes. A high-severity finding still needs an explicit
    # reviewer override even if it is acknowledged/resolved/won't-fix.
    high_ids = list(approval.review_run.findings.filter(
        rule__severity="HIGH"
    ).order_by("id").values_list("id", flat=True))
    reason = (override_reason or "").strip()
    if high_ids and len(reason) < 10:
        raise ValidationError("This review has unresolved high-severity findings. Add an override reason of at least 10 characters to approve this exact version.")
    if len(reason) > 3000:
        raise ValidationError("The override reason must be 3,000 characters or fewer.")
    try:
        decision = _record_decision(
            approval, actor, ApprovalDecision.Action.APPROVED,
            override_reason=reason,
            overridden_finding_ids=[str(item) for item in high_ids] if reason else [],
        )
        approval.status = ApprovalRequest.Status.APPROVED
        approval.save(update_fields=["status"])
        return decision
    except IntegrityError as exc:
        raise ValidationError("A reviewer decision was recorded while you were viewing this request. Reload the page.") from exc


def _locked_request(*, request_id, workspace, actor):
    locked_workspace = Workspace.objects.select_for_update().get(pk=workspace.pk, is_active=True)
    if not _has_role(locked_workspace, actor, REVIEWER_ROLES):
        raise ValidationError("Your workspace role cannot make approval decisions.")
    approval = (
        ApprovalRequest.objects.select_for_update()
        .select_related("workspace", "submission", "content_version", "review_run")
        .filter(pk=request_id, workspace=locked_workspace).first()
    )
    if approval is None:
        raise ApprovalRequest.DoesNotExist
    return approval


def _locked_submission_for_approval(*, submission_id, workspace_id, creator_id):
    """Lock the submission row without joining its nullable current version."""
    return ContentSubmission.objects.select_for_update().filter(
        pk=submission_id,
        workspace_id=workspace_id,
        creator_id=creator_id,
    )


def _ensure_pending(approval, actor):
    if approval.requested_by_id == actor.pk:
        raise ValidationError("A creator cannot review or approve their own content.")
    if approval.status != ApprovalRequest.Status.IN_REVIEW or ApprovalDecision.objects.filter(approval_request=approval).exists():
        raise ValidationError("This request is no longer awaiting a decision. Create and review a new content version.")


def _validate_fresh_review(approval, workspace):
    if approval.submission.current_version_id != approval.content_version_id:
        raise ValidationError("This approval request is stale because a newer content version is current.")
    if approval.content_hash != approval.content_version.content_hash:
        raise ValidationError("The reviewed content hash no longer matches this request.")
    if approval.review_run.content_version_id != approval.content_version_id or not _review_is_complete(approval.review_run):
        raise ValidationError("Only the complete review run linked to this exact version can support approval.")
    current = policy_snapshot(workspace)
    if current != approval.policy_snapshot or current != approval.review_run.policy_snapshot:
        raise ValidationError("Approved brand policy changed after review. The creator must rerun the review and submit a new request.")


def _record_decision(approval, actor, action, *, comment="", override_reason="", overridden_finding_ids=None):
    return ApprovalDecision.objects.create(
        approval_request=approval, reviewer=actor, action=action, comment=comment,
        override_reason=override_reason, overridden_finding_ids=overridden_finding_ids or [],
        content_hash=approval.content_hash, policy_revision=approval.policy_revision,
        policy_snapshot_hash=approval.policy_snapshot_hash, policy_snapshot=approval.policy_snapshot,
    )


def current_valid_approval(submission):
    """Compute whether the latest approval fully covers current content and policy.

    This is the authorization source of truth for protected exports. In addition
    to freshness, verify the immutable decision's links and any high-severity
    override against the original ReviewRun findings.
    """
    approval = (
        submission.approval_requests.filter(status=ApprovalRequest.Status.APPROVED)
        .select_related("content_version", "review_run", "decision")
        .order_by("-decision__decided_at", "-requested_at").first()
    )
    if approval is None:
        return {"valid": False, "approval": None, "decision": None, "reason": "No approved version is on record."}
    decision = getattr(approval, "decision", None)
    if decision is None or decision.action != ApprovalDecision.Action.APPROVED:
        reason = "The approval has no matching persisted human approval decision."
        return {"valid": False, "approval": approval, "decision": decision, "reason": reason}
    if approval.content_version_id != submission.current_version_id:
        reason = "Previous approval is no longer current because the content changed."
    elif (
        approval.content_hash != approval.content_version.content_hash
        or decision.content_hash != approval.content_hash
        or hashlib.sha256(approval.content_version.content_text.encode("utf-8")).hexdigest() != approval.content_hash
    ):
        reason = "Previous approval is no longer current because the content identity changed."
    elif (
        approval.submission_id != submission.pk
        or approval.workspace_id != submission.workspace_id
        or approval.review_run.workspace_id != submission.workspace_id
        or approval.review_run.content_version_id != approval.content_version_id
        or approval.policy_revision != approval.review_run.policy_revision
        or approval.policy_snapshot_hash != approval.review_run.policy_snapshot_hash
        or approval.policy_snapshot != approval.review_run.policy_snapshot
        or decision.policy_revision != approval.policy_revision
        or decision.policy_snapshot_hash != approval.policy_snapshot_hash
        or decision.policy_snapshot != approval.policy_snapshot
    ):
        reason = "The approval, decision and review do not share one exact content and policy identity."
    elif (
        approval.status != ApprovalRequest.Status.APPROVED
        or not _review_is_complete(approval.review_run)
        or approval.review_run.completed_at is None
    ):
        reason = "The historical approval is missing a complete review for the exact version."
    elif policy_snapshot(Workspace.objects.get(pk=submission.workspace_id)) != approval.policy_snapshot:
        reason = "Previous approval is no longer current because the content or approved brand policy changed."
    else:
        high_ids = list(approval.review_run.findings.filter(
            rule__severity="HIGH"
        ).order_by("id").values_list("id", flat=True))
        expected_override_ids = [str(item) for item in high_ids] if decision.override_reason.strip() else []
        if (high_ids and len(decision.override_reason.strip()) < 10) or decision.overridden_finding_ids != expected_override_ids:
            reason = "The recorded high-severity override no longer matches the original review findings."
        else:
            return {"valid": True, "approval": approval, "decision": decision, "reason": "This exact version remains approved against the current policy."}
    return {"valid": False, "approval": approval, "decision": decision, "reason": reason}


def submission_workflow_status(submission):
    """Resolve the current submission state from its latest version-bound request."""
    if not submission.current_version_id:
        return {"state": "DRAFT", "label": "Draft", "approval": None, "stale": False}
    approval = (
        submission.approval_requests.filter(content_version_id=submission.current_version_id)
        .order_by("-requested_at").first()
    )
    if approval is None:
        return {"state": "DRAFT", "label": "Draft", "approval": None, "stale": False}
    if approval.status == ApprovalRequest.Status.IN_REVIEW:
        return {"state": "IN_REVIEW", "label": "In review", "approval": approval, "stale": False}
    if approval.status == ApprovalRequest.Status.CHANGES_REQUESTED:
        return {"state": "CHANGES_REQUESTED", "label": "Changes requested", "approval": approval, "stale": False}
    current = current_valid_approval(submission)
    if current["valid"]:
        return {"state": "APPROVED", "label": "Approved", "approval": approval, "stale": False}
    return {"state": "DRAFT", "label": "Approval no longer current", "approval": approval, "stale": True}
