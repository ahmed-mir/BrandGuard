import hashlib
import json
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator, MinLengthValidator, RegexValidator
from django.db import models
from django.db.models import Q


IDENTIFIER_VALIDATOR = RegexValidator(
    regex=r"\A[A-Z0-9][A-Z0-9._-]{1,39}\Z",
    message="Use 2–40 uppercase letters, numbers, dots, underscores, or hyphens.",
)


def validate_matcher_data(value):
    """Allow only small, known matcher schemas; never accept executable logic."""
    if not isinstance(value, dict):
        raise ValidationError("Matcher configuration must be an object.")

    rule_type = value.get("type")
    schemas = {
        "PROHIBITED_TERM": ({"type", "term", "preferred", "case_sensitive", "scope"}, {"type", "term"}),
        "REQUIRED_TERM": ({"type", "term", "case_sensitive", "scope"}, {"type", "term"}),
        "REQUIRED_DISCLAIMER": ({"type", "phrase", "scope"}, {"type", "phrase", "scope"}),
        "APPROVED_FACT": ({"type", "fact_identifier"}, {"type", "fact_identifier"}),
    }
    if rule_type not in schemas:
        raise ValidationError("Choose a supported deterministic rule type.")
    allowed, required = schemas[rule_type]
    extra = set(value) - allowed
    missing = required - set(value)
    if extra:
        raise ValidationError("Matcher configuration contains unsupported fields.")
    if missing:
        raise ValidationError("Matcher configuration is missing required fields.")
    text_keys = {"term", "preferred", "phrase", "fact_identifier", "scope"}
    for key in text_keys & set(value):
        item = value[key]
        if not isinstance(item, str) or len(item) > 200 or (key in required and not item.strip()):
            raise ValidationError(f"Matcher field '{key}' must be a non-empty string of at most 200 characters.")
    if "case_sensitive" in value and not isinstance(value["case_sensitive"], bool):
        raise ValidationError("Case sensitivity must be a boolean value.")
    if "scope" in value and value["scope"] not in {"ALL", "INSTAGRAM", "LINKEDIN", "EMAIL", "WEBSITE", "PAID_AD"}:
        raise ValidationError("Choose a supported channel scope.")


class BrandSource(models.Model):
    class SourceType(models.TextChoices):
        GUIDELINES = "GUIDELINES", "Guidelines"
        FACT_SHEET = "FACT_SHEET", "Fact sheet"
        POLICY = "POLICY", "Policy"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.PROTECT,
        related_name="brand_sources",
    )
    slug = models.SlugField(max_length=140)
    title = models.CharField(max_length=160)
    source_type = models.CharField(max_length=20, choices=SourceType.choices)
    description = models.CharField(max_length=500, blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_brand_sources",
    )
    is_demo = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["title", "id"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "slug"], name="bs_ws_slug_uniq"),
        ]
        indexes = [models.Index(fields=["workspace", "source_type"], name="bs_ws_type_idx")]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if self.pk:
            old = type(self).objects.filter(pk=self.pk).first()
            if old and old.versions.filter(status=SourceVersion.Status.APPROVED).exists():
                immutable_fields = ("workspace_id", "slug", "title", "source_type", "description", "is_demo")
                if any(getattr(old, field) != getattr(self, field) for field in immutable_fields):
                    raise ValidationError("A source with approved history cannot be rewritten. Create a new source version instead.")
        return super().save(*args, **kwargs)


class SourceVersion(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        APPROVED = "APPROVED", "Approved"
        SUPERSEDED = "SUPERSEDED", "Superseded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(BrandSource, on_delete=models.PROTECT, related_name="versions")
    version_number = models.PositiveIntegerField()
    source_text = models.TextField(validators=[MinLengthValidator(1), MaxLengthValidator(100_000)])
    content_hash = models.CharField(max_length=64, editable=False)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    effective_date = models.DateField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_source_versions",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    is_demo = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version_number"]
        constraints = [
            models.UniqueConstraint(fields=["source", "version_number"], name="sv_src_ver_uniq"),
            models.UniqueConstraint(
                fields=["source"],
                condition=Q(status="APPROVED"),
                name="sv_src_approved_uniq",
            ),
            models.UniqueConstraint(
                fields=["source"],
                condition=Q(status="DRAFT"),
                name="sv_src_draft_uniq",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="DRAFT", approved_by__isnull=True, approved_at__isnull=True)
                    | Q(status__in=["APPROVED", "SUPERSEDED"], approved_by__isnull=False, approved_at__isnull=False)
                ),
                name="sv_approval_meta_ck",
            ),
        ]
        indexes = [
            models.Index(fields=["source", "status"], name="sv_src_status_idx"),
            models.Index(fields=["status", "effective_date"], name="sv_status_date_idx"),
        ]

    def __str__(self):
        return f"{self.source.title} v{self.version_number} ({self.get_status_display()})"

    @property
    def is_active(self):
        return self.status == self.Status.APPROVED

    def clean(self):
        errors = {}
        if not self.source_id:
            errors["source"] = "A source is required for every version."
        if not self.source_text or not self.source_text.strip():
            errors["source_text"] = "Source text cannot be empty."
        if self.status == self.Status.DRAFT and (self.approved_by_id or self.approved_at):
            errors["status"] = "A draft cannot have approval details."
        if self.status in {self.Status.APPROVED, self.Status.SUPERSEDED} and not self.approved_at:
            errors["approved_at"] = "Approval history must include its timestamp."
        if self.status in {self.Status.APPROVED, self.Status.SUPERSEDED} and not self.approved_by_id:
            errors["approved_by"] = "Approval history must identify the approving user."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.content_hash = hashlib.sha256(self.source_text.encode("utf-8")).hexdigest()
        if self.pk:
            old = type(self).objects.filter(pk=self.pk).first()
            if old and old.status != self.Status.DRAFT:
                unchanged = all(
                    getattr(old, field.attname) == getattr(self, field.attname)
                    for field in self._meta.concrete_fields
                    if field.name not in {"status", "content_hash"}
                )
                legal_supersession = old.status == self.Status.APPROVED and self.status == self.Status.SUPERSEDED and unchanged
                if not unchanged or (self.status != old.status and not legal_supersession):
                    raise ValidationError("Approved and superseded source versions are immutable. Create a new draft version.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Source versions are retained for policy history and cannot be deleted.")


class ApprovedFact(models.Model):
    class ComparisonOperator(models.TextChoices):
        UP_TO = "UP_TO", "Up to"
        AT_LEAST = "AT_LEAST", "At least"
        EXACTLY = "EXACTLY", "Exactly"
        RANGE = "RANGE", "Range"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_version = models.ForeignKey(SourceVersion, on_delete=models.PROTECT, related_name="facts")
    identifier = models.CharField(max_length=40, validators=[IDENTIFIER_VALIDATOR])
    subject = models.CharField(max_length=160)
    statement = models.CharField(max_length=1000)
    comparison_operator = models.CharField(max_length=12, choices=ComparisonOperator.choices, blank=True)
    comparison_value = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    comparison_max = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    comparison_unit = models.CharField(max_length=40, blank=True)
    evidence_excerpt = models.TextField(validators=[MinLengthValidator(1), MaxLengthValidator(2000)])
    evidence_start = models.PositiveIntegerField(null=True, blank=True)
    evidence_end = models.PositiveIntegerField(null=True, blank=True)
    effective_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["identifier", "id"]
        constraints = [
            models.UniqueConstraint(fields=["source_version", "identifier"], name="fact_ver_id_uniq"),
            models.CheckConstraint(
                condition=(Q(evidence_start__isnull=True, evidence_end__isnull=True) | Q(evidence_start__isnull=False, evidence_end__isnull=False, evidence_end__gte=models.F("evidence_start"))),
                name="fact_offsets_ck",
            ),
        ]
        indexes = [models.Index(fields=["source_version", "identifier"], name="fact_ver_id_idx")]

    def __str__(self):
        return f"{self.identifier}: {self.subject}"

    @property
    def is_active(self):
        return self.source_version.is_active

    def clean(self):
        errors = {}
        if self.source_version_id and self.source_version.status != SourceVersion.Status.DRAFT:
            errors["source_version"] = "Facts can only be changed on a draft source version."
        if self.evidence_excerpt and self.source_version_id:
            _validate_evidence(self.source_version, self.evidence_excerpt, self.evidence_start, self.evidence_end, errors)
        comparison_values = [self.comparison_operator, self.comparison_value, self.comparison_max, self.comparison_unit]
        if any(comparison_values) and not all((self.comparison_operator, self.comparison_value is not None, self.comparison_unit)):
            errors["comparison_operator"] = "Provide a comparison type, value and unit together, or leave all comparison fields blank."
        if self.comparison_operator == self.ComparisonOperator.RANGE:
            if self.comparison_max is None or (self.comparison_value is not None and self.comparison_max < self.comparison_value):
                errors["comparison_max"] = "A range needs an upper value greater than or equal to its lower value."
        elif self.comparison_max is not None:
            errors["comparison_max"] = "An upper comparison value is only valid for a range."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.source_version.status != SourceVersion.Status.DRAFT:
            raise ValidationError("Approved source evidence cannot be deleted.")
        return super().delete(*args, **kwargs)


class BrandRule(models.Model):
    class Category(models.TextChoices):
        TERMINOLOGY = "TERMINOLOGY", "Terminology"
        VOICE = "VOICE", "Voice"
        DISCLAIMER = "DISCLAIMER", "Disclaimer"
        FACT = "FACT", "Approved fact"
        CLAIM = "CLAIM", "Claims"
        OTHER = "OTHER", "Other"

    class Severity(models.TextChoices):
        LOW = "LOW", "Low"
        MEDIUM = "MEDIUM", "Medium"
        HIGH = "HIGH", "High"

    class RuleType(models.TextChoices):
        PROHIBITED_TERM = "PROHIBITED_TERM", "Prohibited term"
        REQUIRED_TERM = "REQUIRED_TERM", "Required term"
        REQUIRED_DISCLAIMER = "REQUIRED_DISCLAIMER", "Required disclaimer"
        APPROVED_FACT = "APPROVED_FACT", "Approved fact"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_version = models.ForeignKey(SourceVersion, on_delete=models.PROTECT, related_name="rules")
    approved_fact = models.ForeignKey(
        ApprovedFact,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="policy_rules",
    )
    identifier = models.CharField(max_length=40, validators=[IDENTIFIER_VALIDATOR])
    category = models.CharField(max_length=20, choices=Category.choices)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.MEDIUM)
    description = models.CharField(max_length=1000)
    rule_type = models.CharField(max_length=24, choices=RuleType.choices)
    matcher_data = models.JSONField(default=dict, validators=[validate_matcher_data])
    evidence_excerpt = models.TextField(validators=[MinLengthValidator(1), MaxLengthValidator(2000)])
    evidence_start = models.PositiveIntegerField(null=True, blank=True)
    evidence_end = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["identifier", "id"]
        constraints = [
            models.UniqueConstraint(fields=["source_version", "identifier"], name="rule_ver_id_uniq"),
            models.CheckConstraint(
                condition=(Q(evidence_start__isnull=True, evidence_end__isnull=True) | Q(evidence_start__isnull=False, evidence_end__isnull=False, evidence_end__gte=models.F("evidence_start"))),
                name="rule_offsets_ck",
            ),
        ]
        indexes = [models.Index(fields=["source_version", "category", "severity"], name="rule_ver_cat_sev_idx")]

    def __str__(self):
        return f"{self.identifier}: {self.description}"

    @property
    def is_active(self):
        return self.source_version.is_active

    def clean(self):
        errors = {}
        matcher = self.matcher_data if isinstance(self.matcher_data, dict) else {}
        if self.source_version_id and self.source_version.status != SourceVersion.Status.DRAFT:
            errors["source_version"] = "Rules can only be changed on a draft source version."
        if self.evidence_excerpt and self.source_version_id:
            _validate_evidence(self.source_version, self.evidence_excerpt, self.evidence_start, self.evidence_end, errors)
        if self.rule_type == self.RuleType.APPROVED_FACT:
            if not self.approved_fact_id:
                errors["approved_fact"] = "Choose the approved fact this rule represents."
            elif self.approved_fact.source_version_id != self.source_version_id:
                errors["approved_fact"] = "The fact and rule must belong to the same source version."
            elif matcher.get("fact_identifier") != self.approved_fact.identifier:
                errors["matcher_data"] = "The rule must reference its fact identifier."
        elif self.approved_fact_id:
            errors["approved_fact"] = "Only approved-fact rules may reference a fact."
        try:
            validate_matcher_data(self.matcher_data)
            if matcher.get("type") != self.rule_type:
                errors["matcher_data"] = "The matcher type must match the selected rule type."
            if self.rule_type == self.RuleType.APPROVED_FACT and self.approved_fact_id:
                if self.evidence_excerpt != self.approved_fact.evidence_excerpt:
                    errors["evidence_excerpt"] = "Fact-rule evidence must match the linked fact evidence."
        except ValidationError as exc:
            errors["matcher_data"] = exc.messages
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.source_version.status != SourceVersion.Status.DRAFT:
            raise ValidationError("Rules in approved source versions cannot be deleted.")
        return super().delete(*args, **kwargs)


def _validate_evidence(version, excerpt, start, end, errors):
    if not excerpt.strip():
        errors["evidence_excerpt"] = "Enter the exact supporting passage from this source version."
        return
    if (start is None) != (end is None):
        errors["evidence_start"] = "Provide both evidence offsets or leave both blank."
        return
    if start is not None:
        if end < start or version.source_text[start:end] != excerpt:
            errors["evidence_excerpt"] = "The evidence excerpt does not match the selected source text offsets."
        return
    if excerpt not in version.source_text:
        errors["evidence_excerpt"] = "This exact excerpt does not appear in the selected source version. Copy a passage from the source text."


def canonical_policy_data(workspace):
    """Return a stable, JSON-serializable representation of approved policy."""
    approved_versions = SourceVersion.objects.filter(
        source__workspace_id=workspace.pk,
        status=SourceVersion.Status.APPROVED,
    ).select_related("source").prefetch_related("rules", "facts").order_by("source__id", "version_number")
    versions = []
    for version in approved_versions:
        versions.append({
            "source_id": str(version.source_id),
            "version_id": str(version.id),
            "version_number": version.version_number,
            "source_type": version.source.source_type,
            "effective_date": version.effective_date.isoformat() if version.effective_date else None,
            "content_hash": version.content_hash,
            "rules": [
                {
                    "id": rule.identifier,
                    "category": rule.category,
                    "severity": rule.severity,
                    "description": rule.description,
                    "type": rule.rule_type,
                    "matcher": rule.matcher_data,
                    "evidence": rule.evidence_excerpt,
                    "offsets": [rule.evidence_start, rule.evidence_end],
                }
                for rule in sorted(version.rules.all(), key=lambda item: item.identifier)
            ],
            "facts": [
                {
                    "id": fact.identifier,
                    "subject": fact.subject,
                    "statement": fact.statement,
                    "comparison": [fact.comparison_operator, str(fact.comparison_value) if fact.comparison_value is not None else None, str(fact.comparison_max) if fact.comparison_max is not None else None, fact.comparison_unit],
                    "evidence": fact.evidence_excerpt,
                    "offsets": [fact.evidence_start, fact.evidence_end],
                    "effective_date": fact.effective_date.isoformat() if fact.effective_date else None,
                }
                for fact in sorted(version.facts.all(), key=lambda item: item.identifier)
            ],
        })
    return versions


def policy_snapshot_hash(workspace):
    payload = json.dumps(canonical_policy_data(workspace), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
