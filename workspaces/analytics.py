"""Read-only, workspace-scoped policy analytics from persisted records."""
from datetime import datetime, time, timedelta
from statistics import median

from django.db.models import Count
from django.db.models.functions import TruncDate, TruncWeek
from django.utils import timezone
from django.urls import reverse

from approvals.models import ApprovalDecision, ApprovalRequest
from brand_sources.models import BrandRule
from content_reviews.models import (
    ContentSubmission,
    FindingResolutionEvent,
    ReviewFinding,
    ReviewRun,
)
from workspaces.models import WorkspaceMembership


PERIODS = {"7d": 7, "30d": 30, "all": None}
DEFAULT_PERIOD = "30d"


def normalize_period(value):
    """Accept only the small supported window set; invalid input defaults safely."""
    return value if value in PERIODS else DEFAULT_PERIOD


def _format_duration(seconds):
    if seconds < 60:
        return "<1 min"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, remainder = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {remainder}m" if remainder else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def _activity_trend(reviews, *, period, now):
    """Group actual ReviewRun start timestamps into daily or weekly buckets."""
    if period == "7d":
        today = timezone.localdate(now)
        first_day = today - timedelta(days=6)
        start = timezone.make_aware(
            datetime.combine(first_day, time.min), timezone.get_current_timezone()
        )
        bucket_query = reviews.filter(started_at__gte=start, started_at__lte=now).annotate(
            bucket=TruncDate("started_at")
        ).values("bucket").annotate(total=Count("pk")).order_by("bucket")
        buckets = [today - timedelta(days=offset) for offset in reversed(range(7))]
        counts = {row["bucket"]: row["total"] for row in bucket_query}
        rows = [{"label": day.strftime("%a %d"), "count": counts.get(day, 0)} for day in buckets]
        label = "Review starts · last 7 days"
    else:
        days = 30 if period == "30d" else 56
        start = now - timedelta(days=days)
        bucket_query = reviews.filter(started_at__gte=start, started_at__lte=now).annotate(
            bucket=TruncWeek("started_at")
        ).values("bucket").annotate(total=Count("pk")).order_by("bucket")
        local_start = timezone.localdate(start)
        first_week = local_start - timedelta(days=local_start.weekday())
        current_week = timezone.localdate(now)
        current_week -= timedelta(days=current_week.weekday())
        buckets = []
        week = first_week
        while week <= current_week:
            buckets.append(week)
            week += timedelta(weeks=1)
        counts = {row["bucket"].date(): row["total"] for row in bucket_query}
        rows = [{"label": week.strftime("%b %d"), "count": counts.get(week, 0)} for week in buckets]
        label = "Review starts · last 30 days" if period == "30d" else "Review starts · last 8 weeks"

    peak = max((row["count"] for row in rows), default=0)
    for row in rows:
        row["width"] = round(row["count"] * 100 / peak) if peak else 0
    return {"label": label, "rows": rows, "count": sum(row["count"] for row in rows)}


def workspace_analytics(*, workspace, membership, user, period="30d"):
    """Return bounded aggregates from records visible to the authenticated role.

    Finding totals count persisted deterministic finding records, not unique
    real-world issues. Repeated reviews may therefore surface the same rule
    more than once. Approval totals describe historical requests/decisions;
    current approval validity remains the approval service's responsibility.
    """
    period = normalize_period(period)
    now = timezone.now()
    days = PERIODS[period]
    cutoff = now - timedelta(days=days) if days is not None else None

    submissions = ContentSubmission.objects.filter(workspace_id=workspace.pk)
    reviews = ReviewRun.objects.filter(workspace_id=workspace.pk)
    requests = ApprovalRequest.objects.filter(workspace_id=workspace.pk)
    decisions = ApprovalDecision.objects.filter(approval_request__workspace_id=workspace.pk)

    if membership.role == WorkspaceMembership.Role.CREATOR:
        submissions = submissions.filter(creator_id=user.pk)
        reviews = reviews.filter(content_version__submission__creator_id=user.pk)
        requests = requests.filter(requested_by_id=user.pk)
        decisions = decisions.filter(approval_request__requested_by_id=user.pk)

    if cutoff is not None:
        submissions = submissions.filter(created_at__gte=cutoff, created_at__lte=now)
        reviews = reviews.filter(started_at__gte=cutoff, started_at__lte=now)
        requests = requests.filter(requested_at__gte=cutoff, requested_at__lte=now)
        decisions = decisions.filter(decided_at__gte=cutoff, decided_at__lte=now)

    run_ids = reviews.order_by().values("pk")
    findings = ReviewFinding.objects.filter(
        review_run_id__in=run_ids,
        origin=ReviewFinding.Origin.DETERMINISTIC,
    )

    resolution_status_counts = dict(
        findings.order_by().values_list("resolution_status").annotate(total=Count("pk"))
    )
    severity_counts = dict(
        findings.order_by().values_list("rule__severity").annotate(total=Count("pk"))
    )

    classification_counts = dict(
        findings.exclude(classification="").order_by().values_list("classification").annotate(total=Count("pk"))
    )
    classification_total = sum(classification_counts.values())
    classification_labels = dict(ReviewFinding.Classification.choices)
    known_classifications = [key for key, _label in ReviewFinding.Classification.choices]
    classification_order = known_classifications + sorted(set(classification_counts) - set(known_classifications))
    classifications = []
    for key in classification_order:
        count = classification_counts.get(key, 0)
        if count:
            classifications.append({
                "key": key,
                "label": classification_labels.get(key, key.replace("_", " ").title()),
                "count": count,
                "proportion": round(count * 100 / classification_total, 1) if classification_total else None,
            })

    finding_count = findings.count()
    category_counts = findings.values("rule__category").order_by().annotate(
        total=Count("pk")
    ).order_by("-total", "rule__category")
    category_labels = dict(BrandRule.Category.choices)
    categories = [
        {
            "key": row["rule__category"],
            "label": category_labels.get(row["rule__category"], row["rule__category"]),
            "count": row["total"],
            "proportion": round(row["total"] * 100 / finding_count, 1) if finding_count else None,
        }
        for row in category_counts
    ]

    rule_rows = findings.filter(rule__isnull=False).values(
        "rule__identifier", "rule__description", "rule__severity", "rule__category"
    ).order_by().annotate(total=Count("pk")).order_by("-total", "rule__identifier")[:5]
    severity_labels = dict(BrandRule.Severity.choices)
    rules = [
        {
            "identifier": row["rule__identifier"],
            "description": row["rule__description"],
            "severity": severity_labels.get(row["rule__severity"], row["rule__severity"]),
            "category": category_labels.get(row["rule__category"], row["rule__category"]),
            "count": row["total"],
        }
        for row in rule_rows
    ]

    request_status_counts = dict(
        requests.order_by().values_list("status").annotate(total=Count("pk"))
    )
    decision_counts = dict(
        decisions.order_by().values_list("action").annotate(total=Count("pk"))
    )
    decision_labels = dict(ApprovalDecision.Action.choices)
    request_labels = dict(ApprovalRequest.Status.choices)

    durations = []
    for requested_at, decided_at in decisions.order_by().values_list(
        "approval_request__requested_at", "decided_at"
    ).iterator():
        if requested_at and decided_at:
            seconds = (decided_at - requested_at).total_seconds()
            if seconds >= 0:
                durations.append(seconds)
    median_seconds = median(durations) if durations else None

    events = FindingResolutionEvent.objects.filter(
        workspace_id=workspace.pk,
        finding__review_run_id__in=run_ids,
        finding__origin=ReviewFinding.Origin.DETERMINISTIC,
    )
    if cutoff is not None:
        # Event volume is measured by when the resolution action was recorded.
        events = events.filter(created_at__gte=cutoff, created_at__lte=now)

    activity = _activity_trend(reviews, period=period, now=now)
    submission_count = submissions.count()
    review_count = reviews.count()
    completed_review_count = reviews.filter(status=ReviewRun.Status.COMPLETE).count()
    approval_request_count = requests.count()
    decision_count = decisions.count()
    resolution_event_count = events.count()
    return {
        "period": period,
        "period_label": {"7d": "Last 7 days", "30d": "Last 30 days", "all": "All time"}[period],
        "scope_label": "Your submissions" if membership.role == WorkspaceMembership.Role.CREATOR else "Current workspace",
        "next_action_url": reverse(
            "app:content_reviews:create"
            if membership.role == WorkspaceMembership.Role.CREATOR
            else "app:content_reviews:list"
        ),
        "next_action_label": (
            "Create a content submission"
            if membership.role == WorkspaceMembership.Role.CREATOR
            else "Open content reviews"
        ),
        "submission_count": submission_count,
        "review_count": review_count,
        "completed_review_count": completed_review_count,
        "finding_count": finding_count,
        "classification_total": classification_total,
        "classifications": classifications,
        "categories": categories,
        "severities": [
            {"key": key, "label": label, "count": severity_counts.get(key, 0)}
            for key, label in BrandRule.Severity.choices
        ],
        "rules": rules,
        "resolution_states": [
            {"key": key, "label": label, "count": resolution_status_counts.get(key, 0)}
            for key, label in ReviewFinding.ResolutionStatus.choices
        ],
        "resolution_event_count": resolution_event_count,
        "approval_request_count": approval_request_count,
        "request_states": [
            {"key": key, "label": request_labels[key], "count": request_status_counts.get(key, 0)}
            for key, _label in ApprovalRequest.Status.choices
        ],
        "decision_count": decision_count,
        "approved_decision_count": decision_counts.get(ApprovalDecision.Action.APPROVED, 0),
        "decisions": [
            {"key": key, "label": decision_labels[key], "count": decision_counts.get(key, 0)}
            for key, _label in ApprovalDecision.Action.choices
        ],
        "median_decision_seconds": median_seconds,
        "median_decision_time": _format_duration(median_seconds) if median_seconds is not None else None,
        "activity": activity,
        "has_review_records": review_count > 0,
        "has_findings": finding_count > 0,
        "has_approval_requests": approval_request_count > 0,
        "has_decisions": decision_count > 0,
    }
