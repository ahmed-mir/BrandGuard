import hashlib
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q


class AIAnalysisRun(models.Model):
    """An optional, immutable advisory run, separate from deterministic ReviewRun."""

    class Status(models.TextChoices):
        RUNNING = "RUNNING", "Analyzing"
        COMPLETE = "COMPLETE", "Analysis complete"
        FAILED = "FAILED", "Analysis failed"

    class ErrorCategory(models.TextChoices):
        AI_DISABLED = "AI_DISABLED", "AI analysis is disabled"
        MISSING_API_KEY = "MISSING_API_KEY", "Provider key is not configured"
        NO_APPROVED_CONTEXT = "NO_APPROVED_CONTEXT", "No applicable approved source context"
        RATE_LIMITED = "RATE_LIMITED", "Analysis limit reached"
        TIMEOUT = "TIMEOUT", "Provider timed out"
        AUTHENTICATION = "AUTHENTICATION", "Provider credentials rejected"
        QUOTA = "QUOTA", "Provider quota unavailable"
        PROVIDER_RATE_LIMIT = "PROVIDER_RATE_LIMIT", "Provider rate limited the request"
        PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE", "Provider unavailable"
        REFUSAL = "REFUSAL", "Provider declined the request"
        INVALID_RESPONSE = "INVALID_RESPONSE", "Provider response could not be validated"
        INVALID_EVIDENCE = "INVALID_EVIDENCE", "Evidence could not be verified"
        STALE_CONTEXT = "STALE_CONTEXT", "Content or policy changed during analysis"
        PERSISTENCE = "PERSISTENCE", "Analysis could not be saved"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.PROTECT, related_name="ai_analysis_runs")
    content_version = models.ForeignKey(
        "content_reviews.ContentVersion", on_delete=models.PROTECT, related_name="ai_analysis_runs"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="requested_ai_analysis_runs"
    )
    content_hash = models.CharField(max_length=64)
    policy_revision = models.PositiveBigIntegerField()
    policy_snapshot_hash = models.CharField(max_length=64)
    policy_snapshot = models.JSONField(default=dict)
    # This is the bounded set of exact passages sent to the provider, not a saved prompt.
    context_manifest = models.JSONField(default=list, blank=True)
    provider = models.CharField(max_length=24, default="openai")
    model_identifier = models.CharField(max_length=64)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.RUNNING)
    error_category = models.CharField(max_length=32, choices=ErrorCategory.choices, blank=True, default="")
    # Only bounded, allowlisted provider/parser metadata is stored here. Never save
    # prompts, output text, headers, exception messages, or provider payloads.
    error_metadata = models.JSONField(default=dict, blank=True)
    external_processing_consented = models.BooleanField(default=False)
    consented_at = models.DateTimeField(null=True, blank=True)
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    idempotency_key = models.UUIDField(default=uuid.uuid4)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "idempotency_key"], name="ai_run_ws_idempotency_uniq"),
            models.CheckConstraint(
                condition=(
                    Q(status="RUNNING", completed_at__isnull=True)
                    | Q(status__in=["COMPLETE", "FAILED"], completed_at__isnull=False)
                ),
                name="ai_run_completion_meta_ck",
            ),
        ]
        indexes = [
            models.Index(fields=["workspace", "content_version", "created_at"], name="ai_run_ws_cv_date_idx"),
            models.Index(fields=["requested_by", "content_version", "created_at"], name="ai_run_user_cv_idx"),
            models.Index(fields=["status", "created_at"], name="ai_run_status_date_idx"),
        ]

    def clean(self):
        if self.content_version_id:
            version = self.content_version
            if version.submission.workspace_id != self.workspace_id:
                raise ValidationError({"workspace": "The analyzed content must belong to this workspace."})
            if self.content_hash != version.content_hash or hashlib.sha256(version.content_text.encode("utf-8")).hexdigest() != self.content_hash:
                raise ValidationError({"content_hash": "The analysis must identify the exact immutable content version."})
        if self.policy_snapshot and (
            self.policy_snapshot.get("revision") != self.policy_revision
            or self.policy_snapshot.get("hash") != self.policy_snapshot_hash
        ):
            raise ValidationError({"policy_snapshot": "The saved snapshot metadata must match the analysis run."})
        if self.external_processing_consented != bool(self.consented_at):
            raise ValidationError({"consented_at": "Explicit external-processing consent must have a timestamp."})

    def save(self, *args, **kwargs):
        if self.pk:
            old = type(self).objects.filter(pk=self.pk).first()
            if old and old.status != self.Status.RUNNING:
                raise ValidationError("Completed AI analysis runs are immutable.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("AI analysis history is retained and cannot be deleted.")


class AIAnalysisFinding(models.Model):
    class Category(models.TextChoices):
        TONE = "TONE", "Brand tone"
        MESSAGING = "MESSAGING", "Contextual messaging"
        CLAIM = "CLAIM", "Claim requiring human review"

    class Classification(models.TextChoices):
        SUPPORTED = "SUPPORTED", "Supported by supplied evidence"
        POTENTIAL_CONFLICT = "POTENTIAL_CONFLICT", "Potential conflict with supplied evidence"
        NOT_VERIFIED = "NOT_VERIFIED", "Not verified by supplied evidence"
        NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW", "Needs human review"

    class Origin(models.TextChoices):
        AI_JUDGEMENT = "AI_JUDGEMENT", "AI advisory judgement"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    analysis_run = models.ForeignKey(AIAnalysisRun, on_delete=models.PROTECT, related_name="findings")
    origin = models.CharField(max_length=16, choices=Origin.choices, default=Origin.AI_JUDGEMENT)
    category = models.CharField(max_length=12, choices=Category.choices)
    classification = models.CharField(max_length=24, choices=Classification.choices)
    explanation = models.CharField(max_length=1600)
    matched_text = models.CharField(max_length=800, blank=True, default="")
    location_status = models.CharField(max_length=12, choices=[("AVAILABLE", "Available"), ("UNAVAILABLE", "Unavailable")])
    text_start = models.PositiveIntegerField(null=True, blank=True)
    text_end = models.PositiveIntegerField(null=True, blank=True)
    source_version = models.ForeignKey(
        "brand_sources.SourceVersion", on_delete=models.PROTECT, null=True, blank=True, related_name="ai_analysis_findings"
    )
    source_content_hash = models.CharField(max_length=64, blank=True, default="")
    evidence_excerpt = models.TextField(blank=True, default="")
    suggested_wording = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["category", "id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(location_status="UNAVAILABLE", text_start__isnull=True, text_end__isnull=True, matched_text="")
                    | Q(location_status="AVAILABLE", text_start__isnull=False, text_end__gt=F("text_start"))
                ),
                name="ai_finding_location_ck",
            ),
            models.CheckConstraint(
                condition=(
                    Q(source_version__isnull=True, source_content_hash="", evidence_excerpt="")
                    | Q(source_version__isnull=False, source_content_hash__gt="", evidence_excerpt__gt="")
                ),
                name="ai_finding_evidence_ck",
            ),
        ]
        indexes = [models.Index(fields=["analysis_run", "category", "classification"], name="ai_find_run_cat_cls_idx")]

    def clean(self):
        errors = {}
        run = self.analysis_run if self.analysis_run_id else None
        if run:
            version_text = run.content_version.content_text
            if run.status != AIAnalysisRun.Status.RUNNING:
                errors["analysis_run"] = "Findings can only be added while their analysis is running."
            if self.location_status == "AVAILABLE":
                if self.text_start is None or self.text_end is None or version_text[self.text_start:self.text_end] != self.matched_text:
                    errors["matched_text"] = "The matched content must agree with the exact saved text offsets."
            elif self.text_start is not None or self.text_end is not None or self.matched_text:
                errors["location_status"] = "An unavailable location cannot contain a matched span."

        needs_evidence = not (
            self.category == self.Category.CLAIM
            and self.classification in {self.Classification.NOT_VERIFIED, self.Classification.NEEDS_HUMAN_REVIEW}
        )
        if self.source_version_id:
            source = self.source_version
            if not run or source.source.workspace_id != run.workspace_id:
                errors["source_version"] = "Evidence must come from the analyzed workspace."
            snapshot_rows = (run.policy_snapshot or {}).get("versions", []) if run else []
            pinned = next((row for row in snapshot_rows if row.get("version_id") == str(source.pk)), None)
            manifest_rows = (run.context_manifest or []) if run else []
            supplied = any(
                row.get("source_version_id") == str(source.pk)
                and isinstance(row.get("excerpt"), str)
                and self.evidence_excerpt in row.get("excerpt", "")
                for row in manifest_rows
            )
            # An approved source can be superseded while the external request is in
            # flight. Its immutable evidence remains valid for the captured snapshot.
            if source.status not in {"APPROVED", "SUPERSEDED"} or not pinned or not pinned.get("content_hash") == source.content_hash:
                errors["source_version"] = "Evidence must belong to an approved version pinned by this analysis."
            if not self.evidence_excerpt or self.evidence_excerpt not in source.source_text or not supplied:
                errors["evidence_excerpt"] = "Evidence must exactly match text supplied to the model and the approved source."
            if self.source_content_hash != source.content_hash:
                errors["source_content_hash"] = "The evidence content hash must match the approved source version."
        elif needs_evidence or self.source_content_hash or self.evidence_excerpt:
            errors["source_version"] = "This finding requires verifiable approved source evidence."
        elif self.suggested_wording:
            errors["suggested_wording"] = "An uncited claim advisory cannot suggest replacement wording."

        if self.suggested_wording:
            import re
            source_text = self.evidence_excerpt + " " + (run.content_version.content_text if run else "")
            allowed_numbers = set(re.findall(r"\d+(?:[.,]\d+)?%?", source_text))
            suggested_numbers = set(re.findall(r"\d+(?:[.,]\d+)?%?", self.suggested_wording))
            if not suggested_numbers.issubset(allowed_numbers):
                errors["suggested_wording"] = "Suggested wording cannot introduce a numeric claim absent from the content and cited evidence."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("AI analysis findings are retained with their analysis run.")
