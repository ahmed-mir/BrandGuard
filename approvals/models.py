import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class ApprovalRequest(models.Model):
    """A creator's request to decide one exact reviewed content version."""

    class Status(models.TextChoices):
        IN_REVIEW = "IN_REVIEW", "In review"
        CHANGES_REQUESTED = "CHANGES_REQUESTED", "Changes requested"
        APPROVED = "APPROVED", "Approved"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.PROTECT, related_name="approval_requests")
    submission = models.ForeignKey("content_reviews.ContentSubmission", on_delete=models.PROTECT, related_name="approval_requests")
    content_version = models.ForeignKey("content_reviews.ContentVersion", on_delete=models.PROTECT, related_name="approval_requests")
    review_run = models.ForeignKey("content_reviews.ReviewRun", on_delete=models.PROTECT, related_name="approval_requests")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="approval_requests_submitted")
    requested_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.IN_REVIEW)
    content_hash = models.CharField(max_length=64)
    policy_revision = models.PositiveBigIntegerField()
    policy_snapshot_hash = models.CharField(max_length=64)
    policy_snapshot = models.JSONField(default=dict)

    class Meta:
        ordering = ["-requested_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["content_version"], name="approval_one_request_per_version"),
            models.CheckConstraint(condition=Q(status__in=["IN_REVIEW", "CHANGES_REQUESTED", "APPROVED"]), name="approval_valid_status_ck"),
        ]
        indexes = [models.Index(fields=["workspace", "status", "requested_at"], name="approval_ws_status_idx")]

    def __str__(self):
        return f"Approval request for {self.submission.title} v{self.content_version.version_number}"

    def clean(self):
        errors = {}
        if self.submission_id and self.content_version_id and self.content_version.submission_id != self.submission_id:
            errors["content_version"] = "The requested version must belong to this submission."
        if self.submission_id and self.workspace_id and self.submission.workspace_id != self.workspace_id:
            errors["workspace"] = "The request and submission must belong to the same workspace."
        if self.requested_by_id and self.submission_id and self.requested_by_id != self.submission.creator_id:
            errors["requested_by"] = "Only the content creator can submit this version for approval."
        if self.content_version_id and self.content_hash != self.content_version.content_hash:
            errors["content_hash"] = "The request hash must match the immutable content version."
        if self.review_run_id and self.content_version_id and self.review_run.content_version_id != self.content_version_id:
            errors["review_run"] = "The review must apply to the exact requested content version."
        if self.review_run_id and self.workspace_id and self.review_run.workspace_id != self.workspace_id:
            errors["review_run"] = "The review must belong to the request workspace."
        if self.review_run_id:
            if self.policy_revision != self.review_run.policy_revision:
                errors["policy_revision"] = "The request policy revision must match the selected review."
            if self.policy_snapshot_hash != self.review_run.policy_snapshot_hash:
                errors["policy_snapshot_hash"] = "The request policy snapshot must match the selected review."
            if self.policy_snapshot != self.review_run.policy_snapshot:
                errors["policy_snapshot"] = "The request must retain the exact policy snapshot used by the review."
        if self.pk and self.status != self.Status.IN_REVIEW:
            decision = ApprovalDecision.objects.filter(approval_request_id=self.pk).first()
            expected_action = (
                ApprovalDecision.Action.APPROVED
                if self.status == self.Status.APPROVED
                else ApprovalDecision.Action.CHANGES_REQUESTED
            )
            if decision is None or decision.action != expected_action:
                errors["status"] = "A workflow state transition requires its matching immutable reviewer decision."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.pk:
            old = type(self).objects.filter(pk=self.pk).first()
            if old:
                identity_fields = (
                    "workspace_id", "submission_id", "content_version_id", "review_run_id", "requested_by_id",
                    "content_hash", "policy_revision", "policy_snapshot_hash", "policy_snapshot", "requested_at",
                )
                if any(getattr(old, field) != getattr(self, field) for field in identity_fields):
                    raise ValidationError("Approval request identity is immutable.")
                if old.status != self.status and old.status != self.Status.IN_REVIEW:
                    raise ValidationError("A decided request is immutable; create a new content version and request.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Approval request history is retained and cannot be deleted.")


class ApprovalDecision(models.Model):
    class Action(models.TextChoices):
        CHANGES_REQUESTED = "CHANGES_REQUESTED", "Changes requested"
        APPROVED = "APPROVED", "Approved"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    approval_request = models.OneToOneField(ApprovalRequest, on_delete=models.PROTECT, related_name="decision")
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="approval_decisions")
    action = models.CharField(max_length=24, choices=Action.choices)
    comment = models.TextField(max_length=3000, blank=True)
    override_reason = models.TextField(max_length=3000, blank=True)
    overridden_finding_ids = models.JSONField(default=list, blank=True)
    content_hash = models.CharField(max_length=64)
    policy_revision = models.PositiveBigIntegerField()
    policy_snapshot_hash = models.CharField(max_length=64)
    policy_snapshot = models.JSONField(default=dict)
    decided_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-decided_at", "-id"]
        indexes = [models.Index(fields=["reviewer", "decided_at"], name="approval_reviewer_date_idx")]

    def __str__(self):
        return f"{self.get_action_display()} by {self.reviewer}"

    def clean(self):
        errors = {}
        request = self.approval_request if self.approval_request_id else None
        if request:
            if self.content_hash != request.content_hash:
                errors["content_hash"] = "The decision must bind the exact submitted content hash."
            if self.policy_revision != request.policy_revision:
                errors["policy_revision"] = "The decision must bind the requested policy revision."
            if self.policy_snapshot_hash != request.policy_snapshot_hash or self.policy_snapshot != request.policy_snapshot:
                errors["policy_snapshot"] = "The decision must bind the exact requested policy snapshot."
            if self.reviewer_id == request.submission.creator_id:
                errors["reviewer"] = "A creator cannot approve their own submitted content."
            if request.status != ApprovalRequest.Status.IN_REVIEW:
                errors["approval_request"] = "A decision can only be recorded while the request is pending."
            if self.action == self.Action.CHANGES_REQUESTED and len(self.comment.strip()) < 10:
                errors["comment"] = "Add a clear comment of at least 10 characters when requesting changes."
            if self.action == self.Action.APPROVED:
                high_ids = list(request.review_run.findings.filter(
                    rule__severity="HIGH"
                ).order_by("id").values_list("id", flat=True))
                if high_ids and len(self.override_reason.strip()) < 10:
                    errors["override_reason"] = "A meaningful reason is required to override high-severity findings."
                expected_ids = [str(item) for item in high_ids]
                if self.overridden_finding_ids != (expected_ids if self.override_reason.strip() else []):
                    errors["overridden_finding_ids"] = "Override records must identify the exact high-severity findings in the saved review."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Approval decisions are immutable. Record a later decision as a separate request.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Approval decision history is retained and cannot be deleted.")
