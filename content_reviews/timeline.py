"""Read-only lifecycle timeline assembled from saved BrandGuard records."""

from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.urls import reverse
from django.utils.text import Truncator

from approvals.models import ApprovalRequest
from approvals.services import current_valid_approval
from content_reviews.models import ContentVersion, FindingResolutionEvent, ReviewComment, ReviewFinding, ReviewRun
from workspaces.models import WorkspaceMembership


COMMENT_SUMMARY_LIMIT = 180


def build_submission_timeline(*, workspace, submission):
    """Return newest-first events derived only from this workspace's records.

    The returned objects are transient presentation data; this function does
    not write or mutate submissions, reviews, collaboration, approvals or AI.
    """
    if submission.workspace_id != workspace.pk or not workspace.is_active:
        raise PermissionDenied("Timeline events are available only in the active submission workspace.")
    submission.refresh_from_db(fields=["current_version"])

    versions = list(
        ContentVersion.objects.filter(submission_id=submission.pk, submission__workspace_id=workspace.pk)
        .select_related("created_by")
        .order_by("created_at", "version_number", "pk")
    )
    version_ids = [version.pk for version in versions]
    events = []

    def add(*, timestamp, priority, stable_id, event_type, title, description="", actor=None, role="", version=None, url=None):
        if timestamp is None:
            return
        events.append({
            "timestamp": timestamp,
            "event_type": event_type,
            "title": title,
            "description": _summary(description),
            "actor": actor,
            "role": _role_label(role),
            "version_number": version.version_number if version else None,
            "url": url,
            "_priority": priority,
            "_stable_id": str(stable_id),
        })

    add(
        timestamp=submission.created_at,
        priority=10,
        stable_id=submission.pk,
        event_type="submission",
        title="Submission created",
        description=submission.title,
        actor=submission.creator,
        role=WorkspaceMembership.Role.CREATOR,
    )

    for version in versions:
        add(
            timestamp=version.created_at,
            priority=20,
            stable_id=version.pk,
            event_type="version",
            title=f"Version {version.version_number} created",
            description="A new immutable content version was saved.",
            actor=version.created_by,
            role=WorkspaceMembership.Role.CREATOR,
            version=version,
            url=reverse(
                "app:content_reviews:version_detail",
                args=[submission.pk, version.version_number],
            ),
        )

    if version_ids:
        runs = (
            ReviewRun.objects.filter(workspace_id=workspace.pk, content_version_id__in=version_ids)
            .select_related("content_version", "requested_by")
            .annotate(finding_count=Count("findings", distinct=True))
            .order_by("started_at", "pk")
        )
        for run in runs:
            complete = run.status != ReviewRun.Status.RUNNING
            timestamp = run.completed_at if complete and run.completed_at else run.started_at
            if run.status == ReviewRun.Status.COMPLETE:
                title = "Policy review completed"
            elif run.status == ReviewRun.Status.PARTIAL:
                title = "Policy review incomplete"
            elif run.status == ReviewRun.Status.FAILED:
                title = "Policy review failed"
            else:
                title = "Policy review started"
            description = f"{run.finding_count} finding{'s' if run.finding_count != 1 else ''} · policy revision {run.policy_revision}."
            add(
                timestamp=timestamp,
                priority=30,
                stable_id=run.pk,
                event_type="review",
                title=title,
                description=description,
                actor=run.requested_by,
                version=run.content_version,
                url=reverse(
                    "app:content_reviews:version_detail",
                    args=[submission.pk, run.content_version.version_number],
                ),
            )

        comments = (
            ReviewComment.objects.filter(
                workspace_id=workspace.pk,
                submission_id=submission.pk,
                content_version_id__in=version_ids,
            ).filter(
                Q(finding__isnull=True)
                | Q(
                    finding__review_run__workspace_id=workspace.pk,
                    finding__review_run__content_version_id__in=version_ids,
                )
            )
            .select_related("author", "content_version", "finding", "finding__rule")
            .order_by("created_at", "pk")
        )
        for comment in comments:
            if comment.finding_id:
                title = f"Comment on {comment.finding.get_finding_type_display().lower()} finding"
                context = f"Finding: {comment.finding.get_finding_type_display()}. "
            else:
                title = "Review discussion comment added"
                context = ""
            add(
                timestamp=comment.created_at,
                priority=40,
                stable_id=comment.pk,
                event_type="comment",
                title=title,
                description=context + comment.body,
                actor=comment.author,
                role=comment.author_role,
                version=comment.content_version,
                url=reverse(
                    "app:content_reviews:version_detail",
                    args=[submission.pk, comment.content_version.version_number],
                ),
            )

        resolutions = (
            FindingResolutionEvent.objects.filter(
                workspace_id=workspace.pk,
                content_version_id__in=version_ids,
                finding__review_run__workspace_id=workspace.pk,
                finding__review_run__content_version__submission_id=submission.pk,
            )
            .select_related("actor", "content_version", "finding")
            .order_by("created_at", "pk")
        )
        for event in resolutions:
            if event.new_status == "OPEN":
                title = "Finding reopened"
            else:
                status_label = dict(ReviewFinding.ResolutionStatus.choices).get(event.new_status, event.new_status)
                title = f"Finding {status_label.lower()}"
            finding_label = event.finding.get_finding_type_display()
            description = f"{finding_label}."
            if event.note:
                description += f" {event.note}"
            add(
                timestamp=event.created_at,
                priority=50,
                stable_id=event.pk,
                event_type="resolution",
                title=title,
                description=description,
                actor=event.actor,
                role=event.actor_role,
                version=event.content_version,
            )

        approval_requests = (
            ApprovalRequest.objects.filter(workspace_id=workspace.pk, submission_id=submission.pk, content_version_id__in=version_ids)
            .select_related("requested_by", "content_version", "decision", "decision__reviewer")
            .order_by("requested_at", "pk")
        )
        for approval in approval_requests:
            version = approval.content_version
            add(
                timestamp=approval.requested_at,
                priority=60,
                stable_id=approval.pk,
                event_type="approval-request",
                title="Approval requested",
                description="This exact version was submitted for a human decision.",
                actor=approval.requested_by,
                role=WorkspaceMembership.Role.CREATOR,
                version=version,
                url=reverse("app:approvals:detail", args=[approval.pk]),
            )
            decision = getattr(approval, "decision", None)
            if decision:
                approved = decision.action == "APPROVED"
                title = "Version approved" if approved else "Changes requested"
                description = "Human approval recorded for this version."
                if approved and decision.override_reason:
                    description = "Human approval recorded with a documented high-severity override."
                elif not approved and decision.comment:
                    description = decision.comment
                add(
                    timestamp=decision.decided_at,
                    priority=70,
                    stable_id=decision.pk,
                    event_type="approval-decision",
                    title=title,
                    description=description,
                    actor=decision.reviewer,
                    role=WorkspaceMembership.Role.REVIEWER,
                    version=version,
                    url=reverse("app:approvals:detail", args=[approval.pk]),
                )

        # Only completed simulated runs are represented. Real provider calls are
        # intentionally omitted from this lifecycle summary.
        from ai_analysis.models import AIAnalysisRun

        demo_runs = (
            AIAnalysisRun.objects.filter(
                workspace_id=workspace.pk,
                content_version_id__in=version_ids,
                provider="demo",
                status=AIAnalysisRun.Status.COMPLETE,
            )
            .select_related("requested_by", "content_version")
            .order_by("completed_at", "pk")
        )
        for run in demo_runs:
            add(
                timestamp=run.completed_at,
                priority=35,
                stable_id=run.pk,
                event_type="demo-ai",
                title="Demo AI advisory completed · simulated",
                description="Simulated advisory only. No external AI provider was contacted; this is not an approval decision.",
                actor=run.requested_by,
                version=run.content_version,
            )

    events.sort(
        key=lambda event: (event["timestamp"], event["_priority"], event["_stable_id"]),
        reverse=True,
    )
    for event in events:
        event.pop("_priority")
        event.pop("_stable_id")

    stale_approval = None
    if submission.current_version_id:
        validity = current_valid_approval(submission)
        approval = validity["approval"]
        if approval and not validity["valid"]:
            stale_approval = {
                "title": "Approval is no longer current",
                "reason": validity["reason"],
                "url": reverse("app:approvals:detail", args=[approval.pk]),
            }

    return {
        "events": events,
        "stale_approval": stale_approval,
        "ordering_label": "Recent activity · newest first",
    }


def _summary(value):
    compact = " ".join((value or "").split())
    return Truncator(compact).chars(COMMENT_SUMMARY_LIMIT, truncate="…")


def _role_label(role):
    if not role:
        return ""
    return dict(WorkspaceMembership.Role.choices).get(role, role.replace("_", " ").title())
