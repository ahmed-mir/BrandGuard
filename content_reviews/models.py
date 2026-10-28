import hashlib
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator, MinLengthValidator
from django.db import models
from django.db.models import Q


class ContentSubmission(models.Model):
    class Channel(models.TextChoices):
        INSTAGRAM = "INSTAGRAM", "Instagram"
        LINKEDIN = "LINKEDIN", "LinkedIn"
        EMAIL = "EMAIL", "Email"
        WEBSITE = "WEBSITE", "Website"
        PAID_AD = "PAID_AD", "Paid advertisement"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.PROTECT, related_name="content_submissions"
    )
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="content_submissions"
    )
    title = models.CharField(max_length=160)
    channel = models.CharField(max_length=16, choices=Channel.choices)
    audience = models.CharField(max_length=200, blank=True)
    campaign_purpose = models.CharField(max_length=300, blank=True)
    create_idempotency_key = models.UUIDField(null=True, blank=True)
    current_version = models.ForeignKey(
        "content_reviews.ContentVersion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="current_for_submissions",
    )
    is_demo = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "creator", "create_idempotency_key"],
                condition=Q(create_idempotency_key__isnull=False),
                name="cs_create_request_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["workspace", "created_at"], name="cs_ws_created_idx"),
            models.Index(fields=["creator", "created_at"], name="cs_creator_created_idx"),
        ]

    def __str__(self):
        return self.title

    def clean(self):
        if self.current_version_id and self.current_version.submission_id != self.pk:
            raise ValidationError({"current_version": "The current content version must belong to this submission."})


class ContentVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(ContentSubmission, on_delete=models.PROTECT, related_name="versions")
    version_number = models.PositiveIntegerField()
    content_text = models.TextField(validators=[MinLengthValidator(1), MaxLengthValidator(20_000)])
    content_hash = models.CharField(max_length=64, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_content_versions"
    )
    revision_of = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="revision_versions"
    )
    revision_idempotency_key = models.UUIDField(null=True, blank=True)
    is_demo = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version_number"]
        constraints = [
            models.UniqueConstraint(fields=["submission", "version_number"], name="cv_submission_version_uniq"),
            models.UniqueConstraint(
                fields=["submission", "revision_idempotency_key"],
                condition=Q(revision_idempotency_key__isnull=False),
                name="cv_submission_revision_key_uniq",
            ),
        ]
        indexes = [models.Index(fields=["submission", "version_number"], name="cv_submission_ver_idx")]

    def __str__(self):
        return f"{self.submission.title} v{self.version_number}"

    def clean(self):
        if not self.content_text or not self.content_text.strip():
            raise ValidationError({"content_text": "Content text cannot be empty."})
        if self.submission_id and self.created_by_id and self.submission.creator_id != self.created_by_id:
            raise ValidationError({"created_by": "A content version must be created by its submission's creator."})
        if self.revision_of_id:
            if self.revision_of.submission_id != self.submission_id:
                raise ValidationError({"revision_of": "A revision must belong to the same submission."})
            if self.revision_of.version_number >= self.version_number:
                raise ValidationError({"revision_of": "A revision must reference an earlier content version."})

    def save(self, *args, **kwargs):
        self.content_hash = hashlib.sha256(self.content_text.encode("utf-8")).hexdigest()
        if self.pk:
            old = type(self).objects.filter(pk=self.pk).first()
            if old:
                changed = any(
                    getattr(old, field.attname) != getattr(self, field.attname)
                    for field in self._meta.concrete_fields
                    if field.name not in {"content_hash"}
                )
                if changed:
                    raise ValidationError("Saved content versions are immutable. Create a future revision instead.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Content versions and their review history are retained and cannot be deleted.")


class ReviewRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "RUNNING", "Running"
        COMPLETE = "COMPLETE", "Complete"
        PARTIAL = "PARTIAL", "Partial"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.PROTECT, related_name="review_runs")
    content_version = models.ForeignKey(ContentVersion, on_delete=models.PROTECT, related_name="review_runs")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="requested_review_runs"
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.RUNNING)
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    policy_revision = models.PositiveBigIntegerField(default=0)
    policy_snapshot_hash = models.CharField(max_length=64, blank=True, default="")
    policy_snapshot = models.JSONField(default=dict)
    engine_version = models.CharField(max_length=40)
    idempotency_key = models.UUIDField(default=uuid.uuid4)
    failure_code = models.CharField(max_length=40, blank=True, default="")
    failure_message = models.CharField(max_length=500, blank=True, default="")
    incomplete_checks = models.JSONField(default=list)
    not_implemented_checks = models.JSONField(default=list)

    class Meta:
        ordering = ["-started_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "idempotency_key"], name="review_ws_idempotency_uniq"
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="RUNNING", completed_at__isnull=True)
                    | Q(status__in=["COMPLETE", "PARTIAL", "FAILED"], completed_at__isnull=False)
                ),
                name="review_completion_meta_ck",
            ),
        ]
        indexes = [
            models.Index(fields=["workspace", "status", "started_at"], name="review_ws_status_idx"),
            models.Index(fields=["content_version", "started_at"], name="review_cv_started_idx"),
        ]

    def __str__(self):
        return f"Review {self.pk} ({self.get_status_display()})"

    @property
    def is_terminal(self):
        return self.status in {self.Status.COMPLETE, self.Status.PARTIAL, self.Status.FAILED}

    def clean(self):
        if self.content_version_id and self.workspace_id != self.content_version.submission.workspace_id:
            raise ValidationError({"workspace": "The review and content must belong to the same workspace."})

    def save(self, *args, **kwargs):
        if self.pk:
            old = type(self).objects.filter(pk=self.pk).first()
            if old and old.is_terminal:
                changed = any(
                    getattr(old, field.attname) != getattr(self, field.attname)
                    for field in self._meta.concrete_fields
                )
                if changed:
                    raise ValidationError("Completed review records are immutable; create a separate review run.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Review records are retained and cannot be deleted.")


class ReviewPolicyVersion(models.Model):
    class ExclusionReason(models.TextChoices):
        FUTURE_EFFECTIVE = "FUTURE_EFFECTIVE", "Not yet effective"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    review_run = models.ForeignKey(ReviewRun, on_delete=models.PROTECT, related_name="pinned_policy_versions")
    source_version = models.ForeignKey(
        "brand_sources.SourceVersion", on_delete=models.PROTECT, related_name="pinned_review_runs"
    )
    content_hash = models.CharField(max_length=64)
    applied = models.BooleanField(default=True)
    exclusion_reason = models.CharField(max_length=24, choices=ExclusionReason.choices, blank=True)

    class Meta:
        ordering = ["source_version__source__title", "source_version__version_number"]
        constraints = [
            models.UniqueConstraint(fields=["review_run", "source_version"], name="rpolicy_run_sourcever_uniq"),
            models.CheckConstraint(
                condition=(Q(applied=True, exclusion_reason="") | Q(applied=False, exclusion_reason__gt="")),
                name="rpolicy_applied_reason_ck",
            ),
        ]

    def clean(self):
        if self.review_run_id and self.source_version_id:
            if self.source_version.source.workspace_id != self.review_run.workspace_id:
                raise ValidationError("Pinned source versions must belong to the review workspace.")
            if self.content_hash != self.source_version.content_hash:
                raise ValidationError("The pinned content hash must match the immutable approved source version.")

    def save(self, *args, **kwargs):
        if self.review_run_id and self.review_run.is_terminal:
            raise ValidationError("Pinned policy records cannot change after review completion.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Pinned policy history is retained and cannot be deleted.")


class ReviewCheck(models.Model):
    class Outcome(models.TextChoices):
        NO_VIOLATION = "NO_VIOLATION", "No violation found"
        FINDING = "FINDING", "Finding"
        SUPPORTED = "SUPPORTED", "Supported"
        CONTRADICTED = "CONTRADICTED", "Contradicted"
        NOT_VERIFIED = "NOT_VERIFIED", "Not verified"
        NOT_APPLICABLE = "NOT_APPLICABLE", "Not applicable"
        NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW", "Needs human review"
        NOT_COMPLETED = "NOT_COMPLETED", "Not completed"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    review_run = models.ForeignKey(ReviewRun, on_delete=models.PROTECT, related_name="checks")
    rule = models.ForeignKey("brand_sources.BrandRule", on_delete=models.PROTECT, related_name="review_checks")
    source_version = models.ForeignKey(
        "brand_sources.SourceVersion", on_delete=models.PROTECT, related_name="review_checks"
    )
    outcome = models.CharField(max_length=24, choices=Outcome.choices)
    summary = models.CharField(max_length=500)
    error_code = models.CharField(max_length=40, blank=True, default="")
    finding_count = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["rule__category", "rule__identifier", "id"]
        constraints = [
            models.UniqueConstraint(fields=["review_run", "rule"], name="rcheck_run_rule_uniq"),
        ]
        indexes = [models.Index(fields=["review_run", "outcome"], name="rcheck_run_outcome_idx")]

    def clean(self):
        if self.rule_id and self.source_version_id != self.rule.source_version_id:
            raise ValidationError("A check must use the exact source version attached to its rule.")
        if self.review_run_id:
            if self.source_version_id and self.source_version.source.workspace_id != self.review_run.workspace_id:
                raise ValidationError("A check cannot reference policy from another workspace.")
            if not self.review_run.pinned_policy_versions.filter(source_version_id=self.source_version_id, applied=True).exists():
                raise ValidationError("A check must reference a source version in the applied policy snapshot.")

    def save(self, *args, **kwargs):
        if self.review_run_id and self.review_run.is_terminal:
            raise ValidationError("Completed check records are immutable.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Review check history is retained and cannot be deleted.")


class ReviewFinding(models.Model):
    class FindingType(models.TextChoices):
        PROHIBITED_TERM = "PROHIBITED_TERM", "Prohibited terminology"
        REQUIRED_TERM_MISSING = "REQUIRED_TERM_MISSING", "Required terminology missing"
        DISCLAIMER_MISSING = "DISCLAIMER_MISSING", "Required disclaimer missing"
        FACT_CONTRADICTED = "FACT_CONTRADICTED", "Claim contradicts approved fact"
        FACT_NOT_VERIFIED = "FACT_NOT_VERIFIED", "Claim not verified by available facts"

    class Origin(models.TextChoices):
        DETERMINISTIC = "DETERMINISTIC", "Deterministic rule"
        FUTURE_AI = "FUTURE_AI", "Future AI judgement"

    class LocationStatus(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Content location available"
        UNAVAILABLE = "UNAVAILABLE", "Content location unavailable"

    class Classification(models.TextChoices):
        SUPPORTED = "SUPPORTED", "Supported"
        CONTRADICTED = "CONTRADICTED", "Contradicted"
        NOT_VERIFIED = "NOT_VERIFIED", "Not verified"
        NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW", "Needs human review"

    class ResolutionStatus(models.TextChoices):
        OPEN = "OPEN", "Open"
        ACKNOWLEDGED = "ACKNOWLEDGED", "Acknowledged"
        RESOLVED = "RESOLVED", "Resolved"
        WONT_FIX = "WONT_FIX", "Won't fix"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    review_run = models.ForeignKey(ReviewRun, on_delete=models.PROTECT, related_name="findings")
    review_check = models.ForeignKey(ReviewCheck, on_delete=models.PROTECT, related_name="findings")
    rule = models.ForeignKey("brand_sources.BrandRule", on_delete=models.PROTECT, related_name="review_findings")
    source_version = models.ForeignKey(
        "brand_sources.SourceVersion", on_delete=models.PROTECT, related_name="review_findings"
    )
    finding_type = models.CharField(max_length=28, choices=FindingType.choices)
    origin = models.CharField(max_length=16, choices=Origin.choices, default=Origin.DETERMINISTIC)
    classification = models.CharField(max_length=24, choices=Classification.choices, blank=True)
    resolution_status = models.CharField(
        max_length=12,
        choices=ResolutionStatus.choices,
        default=ResolutionStatus.OPEN,
    )
    location_status = models.CharField(max_length=12, choices=LocationStatus.choices)
    text_start = models.PositiveIntegerField(null=True, blank=True)
    text_end = models.PositiveIntegerField(null=True, blank=True)
    matched_text = models.TextField(blank=True)
    explanation = models.TextField(max_length=2000)
    evidence_excerpt = models.TextField()
    suggested_correction = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["rule__severity", "rule__identifier", "text_start", "id"]
        indexes = [
            models.Index(fields=["review_run", "finding_type"], name="rfind_run_type_idx"),
            models.Index(fields=["review_run", "source_version"], name="rfind_run_source_idx"),
        ]

    def clean(self):
        errors = {}
        if self.rule_id and self.source_version_id != self.rule.source_version_id:
            errors["source_version"] = "The cited source must be the exact version attached to the rule."
        if self.review_check_id and self.rule_id and self.review_check.rule_id != self.rule_id:
            errors["review_check"] = "The finding must be produced by its saved rule check."
        if self.review_check_id and self.review_run_id and self.review_check.review_run_id != self.review_run_id:
            errors["review_run"] = "The finding and check must belong to the same review run."
        if self.review_run_id and self.source_version_id:
            if self.source_version.source.workspace_id != self.review_run.workspace_id:
                errors["source_version"] = "The cited source must belong to the review workspace."
            pinned = self.review_run.pinned_policy_versions.filter(
                source_version_id=self.source_version_id, applied=True
            ).exists()
            if not pinned:
                errors["source_version"] = "The cited source must belong to the exact applied policy snapshot."
        if self.evidence_excerpt and self.source_version_id:
            if self.evidence_excerpt != self.rule.evidence_excerpt or self.evidence_excerpt not in self.source_version.source_text:
                errors["evidence_excerpt"] = "The evidence must exactly match the saved rule excerpt in its source version."
        if self.rule_id and self.rule.approved_fact_id:
            if self.evidence_excerpt != self.rule.approved_fact.evidence_excerpt:
                errors["evidence_excerpt"] = "The factual evidence must match the linked approved fact excerpt."

        version = self.review_run.content_version if self.review_run_id else None
        if self.location_status == self.LocationStatus.AVAILABLE:
            if self.text_start is None or self.text_end is None or self.text_end <= self.text_start:
                errors["text_start"] = "A located finding needs a valid start and end offset."
            elif version and version.content_text[self.text_start:self.text_end] != self.matched_text:
                errors["matched_text"] = "The highlighted text does not match these offsets in the saved content version."
        elif self.text_start is not None or self.text_end is not None or self.matched_text:
            errors["location_status"] = "An unavailable content location cannot contain offsets or matched text."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.review_run_id and self.review_run.is_terminal:
            raise ValidationError("Completed findings are immutable.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Review findings are retained and cannot be deleted.")


class ReviewComment(models.Model):
    """Immutable workspace discussion attached to one saved content version.

    A null finding is version-level discussion; a finding reference scopes the
    comment to that exact finding on that exact reviewed version.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.PROTECT, related_name="review_comments"
    )
    submission = models.ForeignKey(
        ContentSubmission, on_delete=models.PROTECT, related_name="review_comments"
    )
    content_version = models.ForeignKey(
        ContentVersion, on_delete=models.PROTECT, related_name="review_comments"
    )
    finding = models.ForeignKey(
        ReviewFinding, null=True, blank=True, on_delete=models.PROTECT, related_name="comments"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="brandguard_review_comments"
    )
    author_role = models.CharField(max_length=20)
    body = models.TextField(max_length=3000)
    idempotency_key = models.UUIDField(default=uuid.uuid4)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "idempotency_key"], name="review_comment_ws_token_uniq"
            ),
        ]
        indexes = [
            models.Index(fields=["workspace", "submission", "content_version", "created_at"], name="rcomment_version_idx"),
            models.Index(fields=["finding", "created_at"], name="rcomment_finding_idx"),
        ]

    def clean(self):
        errors = {}
        if self.submission_id and self.workspace_id != self.submission.workspace_id:
            errors["workspace"] = "The comment and submission must belong to the same workspace."
        if self.content_version_id and self.submission_id and self.content_version.submission_id != self.submission_id:
            errors["content_version"] = "The comment must reference a version of this submission."
        if self.finding_id and self.content_version_id:
            if self.finding.review_run.content_version_id != self.content_version_id:
                errors["finding"] = "The finding must belong to the comment’s exact content version."
            elif self.finding.review_run.workspace_id != self.workspace_id:
                errors["finding"] = "The finding must belong to the comment’s workspace."
        if not self.body or not self.body.strip():
            errors["body"] = "Enter a comment before posting."
        if self.author_role not in {choice for choice, _ in self._role_choices()}:
            errors["author_role"] = "Choose a valid workspace role."
        if errors:
            raise ValidationError(errors)

    @staticmethod
    def _role_choices():
        from workspaces.models import WorkspaceMembership

        return WorkspaceMembership.Role.choices

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Review comments are immutable; add a new comment to continue the discussion.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Review comments are retained and cannot be deleted.")


class FindingResolutionEvent(models.Model):
    """Append-only record of an operational status change for one finding."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.PROTECT, related_name="finding_resolution_events"
    )
    finding = models.ForeignKey(
        ReviewFinding, on_delete=models.PROTECT, related_name="resolution_events"
    )
    content_version = models.ForeignKey(
        ContentVersion, on_delete=models.PROTECT, related_name="finding_resolution_events"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="finding_resolution_events"
    )
    actor_role = models.CharField(max_length=20)
    previous_status = models.CharField(max_length=12, choices=ReviewFinding.ResolutionStatus.choices)
    new_status = models.CharField(max_length=12, choices=ReviewFinding.ResolutionStatus.choices)
    note = models.CharField(max_length=1000, blank=True)
    idempotency_key = models.UUIDField(default=uuid.uuid4)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "idempotency_key"], name="resolution_event_ws_token_uniq"
            ),
            models.CheckConstraint(
                condition=~models.Q(previous_status=models.F("new_status")),
                name="resolution_event_status_change_ck",
            ),
        ]
        indexes = [
            models.Index(fields=["finding", "created_at"], name="resolution_finding_time_idx"),
            models.Index(fields=["workspace", "content_version", "created_at"], name="resolution_version_idx"),
        ]

    def clean(self):
        if self.finding_id and self.content_version_id:
            if self.finding.review_run.content_version_id != self.content_version_id:
                raise ValidationError({"content_version": "The resolution event must stay on the finding’s original version."})
        if self.finding_id and self.workspace_id:
            if self.finding.review_run.workspace_id != self.workspace_id:
                raise ValidationError({"workspace": "The resolution event must belong to the finding’s workspace."})
        if self.previous_status == self.new_status:
            raise ValidationError({"new_status": "Choose a different resolution state."})
        if self.new_status == ReviewFinding.ResolutionStatus.WONT_FIX and len(self.note.strip()) < 10:
            raise ValidationError({"note": "Add a reason of at least 10 characters when marking a finding ‘Won’t fix’."})

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Finding resolution history is immutable; record a new transition instead.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Finding resolution history is retained and cannot be deleted.")
