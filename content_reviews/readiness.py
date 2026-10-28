"""Read-only, role-aware guidance derived from saved review workflow records."""
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.urls import reverse

from approvals.models import ApprovalRequest
from approvals.services import current_valid_approval, review_is_complete
from brand_sources.models import ApprovedFact, BrandRule
from brand_sources.services import policy_snapshot
from content_reviews.models import ReviewFinding, ReviewRun
from workspaces.models import WorkspaceMembership


def build_review_readiness(*, workspace, membership, submission, version, review_runs=None):
    """Build a display-only readiness summary for one visible content version.

    This object is never persisted and grants no access. Mutating views and
    export endpoints continue to perform their own authoritative validation.
    """
    if membership.workspace_id != workspace.pk or not membership.is_active:
        raise PermissionDenied("An active membership in this workspace is required for readiness details.")
    if submission.workspace_id != workspace.pk:
        raise PermissionDenied("Readiness is available only for visible workspace submissions.")
    if version is not None and version.submission_id != submission.pk:
        raise PermissionDenied("Readiness is available only for visible submission versions.")

    # A caller can hold a stale in-memory submission after a revision. Read the
    # current pointer again so the panel never describes an older version as live.
    submission.refresh_from_db(fields=["current_version"])

    role = membership.role
    if not workspace.is_active or not getattr(membership.user, "is_active", False):
        raise PermissionDenied("An active workspace membership is required for readiness details.")
    creator = role == WorkspaceMembership.Role.CREATOR
    can_manage_policy = role in {
        WorkspaceMembership.Role.BRAND_MANAGER,
        WorkspaceMembership.Role.ADMIN,
    }
    can_review = role in {
        WorkspaceMembership.Role.REVIEWER,
        WorkspaceMembership.Role.ADMIN,
    }
    is_current = bool(version and submission.current_version_id == version.pk)

    # Do not trust a cached Workspace instance passed by a long-lived caller.
    # Refresh only its policy identity before comparing pinned review snapshots.
    workspace.refresh_from_db(fields=["policy_revision", "policy_snapshot_hash"])
    snapshot = policy_snapshot(workspace)
    version_ids = [item["version_id"] for item in snapshot["versions"]]
    policy = {
        "ready": bool(version_ids),
        "label": "Policy ready" if version_ids else "No approved policy",
        "revision": snapshot["revision"],
        "source_count": len(version_ids),
        "rule_count": BrandRule.objects.filter(source_version_id__in=version_ids).count() if version_ids else 0,
        "fact_count": ApprovedFact.objects.filter(source_version_id__in=version_ids).count() if version_ids else 0,
        "stale_for_review": False,
    }

    review_history = [run for run in list(
        review_runs if review_runs is not None
        else (version.review_runs.select_related("requested_by") if version is not None else [])
    ) if version is not None and run.workspace_id == workspace.pk and run.content_version_id == version.pk]
    latest_review = review_history[0] if review_history else None
    eligible_review = next((
        run for run in review_history
        if is_current and run.policy_snapshot == snapshot and review_is_complete(run)
    ), None)
    relevant_review = eligible_review or latest_review
    review_fresh = eligible_review is not None
    if relevant_review is None:
        review_state = {
            "key": "NOT_REVIEWED", "label": "Not reviewed", "run": None,
            "fresh": False, "count": 0, "high_count": 0, "resolutions": _empty_resolution_counts(),
            "resolution_rows": [
                {"label": label, "count": 0}
                for _status, label in ReviewFinding.ResolutionStatus.choices
            ],
        }
    else:
        stale_policy = relevant_review.policy_snapshot != snapshot
        policy["stale_for_review"] = stale_policy
        if not is_current:
            label, key = f"Historical review · {relevant_review.get_status_display().lower()}", "HISTORICAL"
        elif relevant_review.status == ReviewRun.Status.COMPLETE and stale_policy:
            label, key = "Policy changed since this review", "STALE"
        elif relevant_review.status == ReviewRun.Status.PARTIAL:
            label, key = "Review incomplete", "PARTIAL"
        elif relevant_review.status == ReviewRun.Status.FAILED:
            label, key = "Review failed", "FAILED"
        elif relevant_review.status == ReviewRun.Status.RUNNING:
            label, key = "Review running", "RUNNING"
        elif eligible_review and relevant_review.pk != latest_review.pk:
            label, key = "Complete review available", "COMPLETE"
        else:
            label, key = "Review complete", "COMPLETE"
        counts = relevant_review.findings.aggregate(
            total=Count("pk"),
            high=Count("pk", filter=Q(rule__severity=BrandRule.Severity.HIGH)),
            **{
                f"resolution_{status.lower()}": Count("pk", filter=Q(resolution_status=status))
                for status, _ in ReviewFinding.ResolutionStatus.choices
            },
        )
        review_state = {
            "key": key,
            "label": label,
            "run": relevant_review,
            "latest_run": latest_review,
            "fresh": review_fresh,
            "count": counts["total"] or 0,
            "high_count": counts["high"] or 0,
            "resolutions": {
                status: counts[f"resolution_{status.lower()}"] or 0
                for status, _ in ReviewFinding.ResolutionStatus.choices
            },
            "resolution_rows": [
                {"label": label, "count": counts[f"resolution_{status.lower()}"] or 0}
                for status, label in ReviewFinding.ResolutionStatus.choices
            ],
        }

    # Approval request state belongs to the exact version currently displayed.
    approval_request = (
        version.approval_requests.select_related("review_run", "requested_by")
        .order_by("-requested_at", "-id").first()
        if version is not None else None
    )
    approval_state = current_valid_approval(submission)
    request_is_stale = False
    if approval_request is not None:
        request_is_stale = bool(
            not is_current
            or approval_request.content_hash != version.content_hash
            or approval_request.policy_snapshot != snapshot
            or not review_is_complete(approval_request.review_run)
            or approval_request.review_run.content_version_id != version.pk
        )
    approval_high_count = (
        approval_request.review_run.findings.filter(rule__severity=BrandRule.Severity.HIGH).count()
        if approval_request else review_state["high_count"]
    )
    valid_for_version = bool(
        is_current
        and approval_state["valid"]
        and approval_state["approval"]
        and approval_request
        and approval_state["approval"].pk == approval_request.pk
    )
    if valid_for_version:
        approval = {"key": "APPROVED", "label": "Approved", "request": approval_request, "stale": False, "high_count": approval_high_count}
    elif approval_request and approval_request.status == ApprovalRequest.Status.IN_REVIEW:
        if request_is_stale:
            approval = {"key": "STALE", "label": "Request is stale", "request": approval_request, "stale": True, "high_count": approval_high_count}
        else:
            approval = {"key": "PENDING", "label": "Awaiting reviewer", "request": approval_request, "stale": False, "high_count": approval_high_count}
    elif approval_request and approval_request.status == ApprovalRequest.Status.CHANGES_REQUESTED:
        approval = {"key": "CHANGES_REQUESTED", "label": "Changes requested", "request": approval_request, "stale": request_is_stale, "high_count": approval_high_count}
    elif approval_request and approval_request.status == ApprovalRequest.Status.APPROVED:
        approval = {"key": "STALE", "label": "Approval no longer current", "request": approval_request, "stale": True, "high_count": approval_high_count}
    elif is_current and approval_state["approval"] is not None:
        approval = {"key": "STALE", "label": "Approval no longer current", "request": approval_state["approval"], "stale": True, "high_count": approval_high_count}
    else:
        approval = {"key": "NOT_REQUESTED", "label": "Not requested", "request": None, "stale": False, "high_count": approval_high_count}
    can_create_current_request = approval["key"] == "NOT_REQUESTED" or (
        approval["key"] == "STALE"
        and approval["request"] is not None
        and approval["request"].content_version_id != (version.pk if version else None)
    )
    approval["override_required"] = bool(
        approval_high_count
        and (approval["key"] == "PENDING" or (can_create_current_request and review_fresh))
    )

    export_available = valid_for_version
    export_state = {
        "available": export_available,
        "label": "Available" if export_available else "Locked - current approval required",
        "reason": (
            "This exact version has a current human approval."
            if export_available
            else (approval_state["reason"] if approval_state["approval"] else "A current human approval is required before export.")
        ),
    }

    stage = _stage(
        version=version,
        is_current=is_current,
        policy=policy,
        review=review_state,
        approval=approval,
        export_available=export_available,
    )
    next_action = _next_action(
        creator=creator,
        can_manage_policy=can_manage_policy,
        can_review=can_review,
        submission=submission,
        version=version,
        is_current=is_current,
        policy=policy,
        review=review_state,
        approval=approval,
        export=export_state,
    )
    if review_state["run"] and not policy["ready"]:
        policy["stale_for_review"] = True
        policy["label"] = "No approved policy for this review"
    elif policy["stale_for_review"]:
        policy["label"] = "Policy changed since review"

    return {
        "stage": stage["key"],
        "stage_label": stage["label"],
        "stage_explanation": stage["explanation"],
        "is_current": is_current,
        "policy": policy,
        "review": review_state,
        "approval": approval,
        "export": export_state,
        "next_action": next_action,
        "can_run_review": bool(creator and is_current and policy["ready"] and approval_request is None),
        "resolution_note": "Finding resolution tracks collaboration state and does not equal approval.",
    }


def _empty_resolution_counts():
    return {status: 0 for status, _ in ReviewFinding.ResolutionStatus.choices}


def _stage(*, version, is_current, policy, review, approval, export_available):
    if version is None:
        return {"key": "DRAFT", "label": "Draft", "explanation": "Create content to begin a policy review."}
    if not is_current:
        return {"key": "HISTORICAL", "label": "Historical version", "explanation": "This saved version remains available as history; actions apply to the current version."}
    if approval["key"] == "STALE":
        return {"key": "APPROVAL_STALE", "label": "Approval stale", "explanation": "This approval no longer covers the current content version or policy. A fresh review and current approval are required before export."}
    if approval["key"] == "CHANGES_REQUESTED":
        return {"key": "CHANGES_REQUESTED", "label": "Changes requested", "explanation": "Review the feedback and prepare a new content version."}
    if approval["key"] == "PENDING":
        return {"key": "APPROVAL_PENDING", "label": "Approval pending", "explanation": "This exact version is waiting for a reviewer decision."}
    if approval["key"] == "APPROVED" and export_available:
        return {"key": "READY_TO_EXPORT", "label": "Ready to export", "explanation": "This exact content version is currently approved."}
    if not policy["ready"]:
        return {"key": "POLICY_REQUIRED", "label": "Policy setup needed", "explanation": "An approved brand source is needed before this content can be reviewed."}
    if review["key"] == "STALE":
        return {"key": "REVIEW_STALE", "label": "Review needs refreshing", "explanation": "The approved policy changed after this review. Run a fresh review before requesting approval."}
    if review["key"] == "FAILED":
        return {"key": "REVIEW_FAILED", "label": "Review failed", "explanation": "The review did not complete. Check its saved result and run a new review."}
    if review["key"] == "PARTIAL":
        return {"key": "REVIEW_PARTIAL", "label": "Review incomplete", "explanation": "Some policy checks did not complete. Do not treat this as a complete review."}
    if review["key"] == "RUNNING":
        return {"key": "REVIEW_RUNNING", "label": "Review running", "explanation": "The deterministic policy review is still processing."}
    if review["fresh"] and review["count"]:
        return {"key": "REVISION_RECOMMENDED", "label": "Findings need attention", "explanation": "Review the findings and their evidence. Revise the content where needed before requesting approval."}
    if review["fresh"]:
        return {"key": "READY_FOR_APPROVAL", "label": "Ready to request approval", "explanation": "A complete deterministic review matches the current approved policy."}
    return {"key": "READY_FOR_REVIEW", "label": "Ready for review", "explanation": "Run the deterministic policy review against the current approved brand sources."}


def _next_action(
    *, creator, can_manage_policy, can_review, submission, version,
    is_current, policy, review, approval, export,
):
    if version is None:
        return _action(
            "Create content to begin review.",
            "New content" if creator else None,
            reverse("app:content_reviews:create") if creator else None,
        )
    if not is_current:
        current_version_url = (
            reverse(
                "app:content_reviews:version_detail",
                args=[submission.pk, submission.current_version.version_number],
            )
            if submission.current_version_id else None
        )
        return _action(
            "This version is historical. Open the current version to continue the workflow.",
            "Open current version",
            current_version_url,
        )
    if not policy["ready"]:
        if can_manage_policy:
            return _action("Publish an approved Brand Source to enable policy reviews.", "Open Brand Sources", reverse("app:brand_sources:list"))
        return _action("Ask a brand manager to publish an approved Brand Source before review can begin.", None)
    if approval["key"] == "STALE":
        request = approval["request"]
        same_version_request = bool(request and request.content_version_id == version.pk)
        if creator and same_version_request:
            return _action("This version already has an approval request. Create a new version before requesting approval again.", "Create a new version", reverse("app:content_reviews:revise", args=[submission.pk]))
        if creator:
            if review["fresh"]:
                if review["count"]:
                    return _action(
                        "The earlier approval is stale. Inspect the current findings and evidence before requesting a new decision.",
                        "Inspect review findings",
                        reverse("app:content_reviews:review_detail", args=[submission.pk, version.version_number, review["run"].pk]),
                    )
                return _action("The earlier approval is stale. Submit this freshly reviewed version for a new human decision.", "Go to approval submission", "#readiness-submit")
            return _action("Run a fresh deterministic review for this current version, then request approval.", "Run policy review", "#readiness-review-form")
        return _action("A fresh review and approval are required for the current version before export.", None)
    if approval["key"] == "CHANGES_REQUESTED":
        if creator:
            return _action("Review the feedback and create a revised content version.", "Revise content", reverse("app:content_reviews:revise", args=[submission.pk]))
        return _action("The reviewer requested changes. The creator can prepare a new version.", None)
    if approval["key"] == "PENDING":
        if can_review:
            return _action("Inspect this version's evidence and submit a human decision.", "Review approval request", reverse("app:approvals:detail", args=[approval["request"].pk]))
        return _action("A reviewer must decide on this exact content version before export is available.", None)
    if approval["key"] == "APPROVED" and export["available"]:
        return _action("This exact version is approved and ready to export.", "Go to exports", "#readiness-exports")
    if review["key"] == "RUNNING":
        return _action("The deterministic review is still running. Refresh this page to see its saved result.", None)
    if review["key"] in {"FAILED", "PARTIAL", "STALE"}:
        return _action(
            "Run a new deterministic review against the current approved policy.",
            "Run policy review" if creator else None,
            "#readiness-review-form" if creator else None,
        )
    if review["fresh"]:
        if creator:
            if review["count"]:
                return _action(
                    "Inspect the deterministic findings and evidence. Revise where needed before submitting for approval.",
                    "Inspect review findings",
                    reverse("app:content_reviews:review_detail", args=[submission.pk, version.version_number, review["run"].pk]),
                )
            return _action("Review the saved findings, then submit this exact version for approval.", "Go to approval submission", "#readiness-submit")
        return _action("The creator can submit this reviewed version for approval.", None)
    if creator:
        return _action("Run the deterministic policy review for this version.", "Run policy review", "#readiness-review-form")
    return _action("The creator must run a review before requesting approval.", None)


def _action(description, label, url=None):
    return {"description": description, "label": label, "url": url}
