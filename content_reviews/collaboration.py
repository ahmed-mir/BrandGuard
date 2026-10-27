"""Workspace-scoped discussion and append-only finding resolution services."""
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError, connection, transaction

from workspaces.models import Workspace, WorkspaceMembership

from .models import (
    ContentSubmission,
    ContentVersion,
    FindingResolutionEvent,
    ReviewComment,
    ReviewFinding,
)


ROLE_CREATOR = WorkspaceMembership.Role.CREATOR
REVIEWER_ROLES = {WorkspaceMembership.Role.REVIEWER, WorkspaceMembership.Role.ADMIN}
ALL_ROLES = {role for role, _ in WorkspaceMembership.Role.choices}

ALLOWED_RESOLUTION_TRANSITIONS = {
    ReviewFinding.ResolutionStatus.OPEN: {
        ReviewFinding.ResolutionStatus.ACKNOWLEDGED,
        ReviewFinding.ResolutionStatus.RESOLVED,
        ReviewFinding.ResolutionStatus.WONT_FIX,
    },
    ReviewFinding.ResolutionStatus.ACKNOWLEDGED: {
        ReviewFinding.ResolutionStatus.OPEN,
        ReviewFinding.ResolutionStatus.RESOLVED,
        ReviewFinding.ResolutionStatus.WONT_FIX,
    },
    ReviewFinding.ResolutionStatus.RESOLVED: {ReviewFinding.ResolutionStatus.OPEN},
    ReviewFinding.ResolutionStatus.WONT_FIX: {ReviewFinding.ResolutionStatus.OPEN},
}


def _uuid(value, label):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError(f"Refresh the page and try again; the {label} is invalid.") from exc


def _membership(*, workspace, actor):
    membership = WorkspaceMembership.objects.filter(
        workspace_id=workspace.pk,
        workspace__is_active=True,
        user=actor,
        user__is_active=True,
        is_active=True,
    ).first()
    if membership is None:
        raise PermissionDenied("An active membership in this workspace is required.")
    return membership


def _visible_submission(*, workspace, actor, membership, submission_id):
    submission = ContentSubmission.objects.filter(
        pk=submission_id, workspace_id=workspace.pk, workspace__is_active=True
    ).first()
    if submission is None or (membership.role == ROLE_CREATOR and submission.creator_id != actor.pk):
        raise ContentSubmission.DoesNotExist
    return submission


def _same_comment(comment, *, submission, version, finding, actor, body):
    return (
        comment.submission_id == submission.pk
        and comment.content_version_id == version.pk
        and comment.finding_id == (finding.pk if finding else None)
        and comment.author_id == actor.pk
        and comment.body == body
    )


def add_review_comment(
    *, workspace, actor, submission_id, version_number, body, idempotency_key, finding_id=None
):
    """Append a version-level or finding-specific comment exactly once per token."""
    token = _uuid(idempotency_key, "request token")
    submission_key = _uuid(submission_id, "submission")
    text = (body or "").strip()
    if not text:
        raise ValidationError({"body": "Enter a comment before posting."})
    if len(text) > 3000:
        raise ValidationError({"body": "Comments must be 3,000 characters or fewer."})

    membership = _membership(workspace=workspace, actor=actor)
    if membership.role not in ALL_ROLES:
        raise PermissionDenied("Your workspace role cannot comment on review content.")
    submission = _visible_submission(
        workspace=workspace, actor=actor, membership=membership, submission_id=submission_key
    )
    version = ContentVersion.objects.filter(
        submission=submission, version_number=version_number
    ).first()
    if version is None:
        raise ContentVersion.DoesNotExist
    finding = None
    if finding_id is not None:
        finding_key = _uuid(finding_id, "finding")
        finding = ReviewFinding.objects.filter(
            pk=finding_key,
            review_run__workspace_id=workspace.pk,
            review_run__content_version=version,
        ).first()
        if finding is None:
            raise ReviewFinding.DoesNotExist

    existing = ReviewComment.objects.filter(workspace=workspace, idempotency_key=token).first()
    if existing:
        if _same_comment(existing, submission=submission, version=version, finding=finding, actor=actor, body=text):
            return existing, False
        raise ValidationError("This comment request token was already used. Refresh before posting again.")

    comment = ReviewComment(
        workspace=workspace,
        submission=submission,
        content_version=version,
        finding=finding,
        author=actor,
        author_role=membership.role,
        body=text,
        idempotency_key=token,
    )
    try:
        with transaction.atomic():
            comment.full_clean()
            comment.save()
    except IntegrityError as exc:
        existing = ReviewComment.objects.filter(workspace=workspace, idempotency_key=token).first()
        if existing and _same_comment(existing, submission=submission, version=version, finding=finding, actor=actor, body=text):
            return existing, False
        raise ValidationError("This comment was already submitted or the page is stale. Reload and try again.") from exc
    return comment, True


def resolution_choices(*, membership, finding, submission):
    """Return permitted transition targets; services still validate every POST."""
    current = finding.resolution_status
    targets = ALLOWED_RESOLUTION_TRANSITIONS.get(current, set())
    if membership.role == ROLE_CREATOR:
        targets = targets & {
            ReviewFinding.ResolutionStatus.OPEN,
            ReviewFinding.ResolutionStatus.ACKNOWLEDGED,
            ReviewFinding.ResolutionStatus.RESOLVED,
        }
        if (
            finding.review_run.content_version_id == submission.current_version_id
            and ReviewFinding.ResolutionStatus.RESOLVED in targets
        ):
            targets = targets - {ReviewFinding.ResolutionStatus.RESOLVED}
    return [(value, label) for value, label in ReviewFinding.ResolutionStatus.choices if value in targets]


def change_finding_resolution(
    *, workspace, actor, submission_id, version_number, finding_id,
    expected_status, new_status, note="", idempotency_key,
):
    """Atomically update the convenience status and append its audit event."""
    token = _uuid(idempotency_key, "request token")
    submission_key = _uuid(submission_id, "submission")
    finding_key = _uuid(finding_id, "finding")
    note = (note or "").strip()

    try:
        with transaction.atomic():
            locked_workspace = Workspace.objects.select_for_update().get(
                pk=workspace.pk, is_active=True
            )
            membership = _membership(workspace=locked_workspace, actor=actor)
            submission = _visible_submission(
                workspace=locked_workspace, actor=actor, membership=membership,
                submission_id=submission_key,
            )
            version = ContentVersion.objects.filter(
                submission=submission, version_number=version_number
            ).first()
            if version is None:
                raise ContentVersion.DoesNotExist

            existing = FindingResolutionEvent.objects.filter(
                workspace=locked_workspace, idempotency_key=token
            ).first()
            if existing:
                if (
                    existing.finding_id == finding_key
                    and existing.actor_id == actor.pk
                    and existing.new_status == new_status
                    and existing.note == note
                ):
                    return existing, False
                raise ValidationError("This resolution request token was already used. Reload the finding.")

            # Lock only the finding row plus its required, non-null review/version
            # relations. No nullable join is added to this PostgreSQL FOR UPDATE.
            finding = ReviewFinding.objects.select_for_update().filter(
                pk=finding_key,
                review_run__workspace_id=locked_workspace.pk,
                review_run__content_version=version,
            ).first()
            if finding is None:
                raise ReviewFinding.DoesNotExist

            if membership.role not in ALL_ROLES:
                raise PermissionDenied("Your workspace role cannot update finding status.")
            if new_status not in ReviewFinding.ResolutionStatus.values:
                raise ValidationError({"new_status": "Choose a valid finding status."})

            current = finding.resolution_status
            # A retry with a fresh form/token is a safe no-op when the requested
            # state has already become current; it never creates another event.
            if current == new_status:
                return None, False
            if expected_status != current:
                raise ValidationError("This finding changed in another session. Reload its latest history before updating it.")
            if new_status not in ALLOWED_RESOLUTION_TRANSITIONS.get(current, set()):
                raise ValidationError(f"The transition from {finding.get_resolution_status_display()} to the requested state is not allowed.")
            if membership.role == ROLE_CREATOR:
                if new_status == ReviewFinding.ResolutionStatus.WONT_FIX:
                    raise PermissionDenied("Creators cannot mark a policy finding as ‘Won’t fix’.")
                if (
                    new_status == ReviewFinding.ResolutionStatus.RESOLVED
                    and submission.current_version_id == version.pk
                ):
                    raise ValidationError("Save a newer content version before marking this finding resolved.")
            if new_status == ReviewFinding.ResolutionStatus.RESOLVED and len(note) < 5:
                raise ValidationError({"note": "Add a short note describing how this finding was addressed."})
            if new_status == ReviewFinding.ResolutionStatus.WONT_FIX and len(note) < 10:
                raise ValidationError({"note": "Add a reason of at least 10 characters when marking a finding ‘Won’t fix’."})
            if len(note) > 1000:
                raise ValidationError({"note": "Resolution notes must be 1,000 characters or fewer."})

            event = FindingResolutionEvent(
                workspace=locked_workspace,
                finding=finding,
                content_version=version,
                actor=actor,
                actor_role=membership.role,
                previous_status=current,
                new_status=new_status,
                note=note,
                idempotency_key=token,
            )
            event.full_clean()
            event.save()
            updated = ReviewFinding.objects.filter(pk=finding.pk, resolution_status=current).update(
                resolution_status=new_status
            )
            if updated != 1:
                raise ValidationError("This finding changed before the update was saved. Reload and try again.")
            return event, True
    except IntegrityError as exc:
        existing = FindingResolutionEvent.objects.filter(workspace_id=workspace.pk, idempotency_key=token).first()
        if existing and existing.finding_id == finding_key and existing.actor_id == actor.pk and existing.new_status == new_status and existing.note == note:
            return existing, False
        raise ValidationError("Another status update was saved first. Reload the finding history and try again.") from exc
    except OperationalError as exc:
        if connection.vendor == "sqlite" and "locked" in str(exc).casefold():
            raise ValidationError("Another update is being saved. Reload the finding history and try again.") from exc
        raise
