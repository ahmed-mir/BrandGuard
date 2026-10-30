"""Source-grounded, optional AI advisory workflow, isolated from deterministic reviews."""
from datetime import timedelta
import logging
import re
import uuid

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from brand_sources.models import BrandRule, SourceVersion
from brand_sources.services import active_policy_versions, policy_snapshot
from content_reviews.models import ContentSubmission, ContentVersion
from workspaces.models import Workspace, WorkspaceMembership

from .models import AIAnalysisFinding, AIAnalysisRun
from .provider import ProviderFailure, request_analysis
from .schemas import FINDING_SCHEMA


logger = logging.getLogger(__name__)
ENGINE_VERSION = "source-grounded-advisory-1.0"
MAX_ANALYSES_PER_USER_VERSION_PER_HOUR = 3
MAX_CONTENT_CHARS = 20_000
_WORD_RE = re.compile(r"[\w'-]+", re.UNICODE)
_STOP_WORDS = {
    "about", "after", "against", "also", "and", "are", "because", "been", "being", "between", "but",
    "can", "could", "does", "for", "from", "have", "into", "its", "more", "most", "must", "not",
    "our", "ours", "should", "that", "the", "their", "them", "then", "there", "these", "they",
    "this", "those", "through", "under", "using", "was", "were", "what", "when", "where", "which",
    "while", "with", "would", "your", "you", "than", "such", "will", "yourself",
}
_SAFE_ERROR_STAGES = {
    "provider_http_error", "response_status", "incomplete_response", "refusal", "missing_text",
    "json_decode", "provider_schema_contract", "domain_parse", "domain_semantic",
    "evidence_validation", "schema_validation", "unsupported_response_shape",
}
_SAFE_VALIDATION_CODES = {
    "wrong_type", "missing_key", "unexpected_key", "invalid_enum", "invalid_uuid",
    "empty_value", "too_long", "unsupported_combination", "too_many_findings",
    "unmatched_content", "unsupported_numeric_claim", "invalid_citation",
}
_SAFE_RESPONSE_STATUSES = {"completed", "incomplete", "failed", "queued", "in_progress", "cancelled", "unknown"}
_SAFE_OUTPUT_TYPES = {"message", "reasoning", "function_call", "web_search_call", "other"}
_SAFE_INCOMPLETE_REASONS = {"max_output_tokens", "content_filter", "other"}
_SAFE_FINISH_REASONS = {"stop", "length", "content_filter", "tool_calls", "function_call", "other"}


class AIAnalysisRejected(Exception):
    """Safe, user-displayable analysis rejection with a persisted error category."""

    def __init__(self, category, message, *, parser_stage=None, validation_errors=None, validation_error_count=None):
        self.category = category
        self.public_message = message
        self.parser_stage = parser_stage
        self.validation_errors = validation_errors or []
        self.validation_error_count = validation_error_count if validation_error_count is not None else len(self.validation_errors)
        super().__init__(message)


def ai_analysis_available():
    return ai_analysis_mode() in {"demo", "real"}


def ai_analysis_mode():
    """Return the one unambiguous execution mode configured for this process."""
    if settings.BRANDGUARD_AI_ENABLED and settings.BRANDGUARD_AI_DEMO_MODE:
        return "conflict"
    if settings.BRANDGUARD_AI_DEMO_MODE:
        return "demo"
    if settings.BRANDGUARD_AI_ENABLED:
        if settings.BRANDGUARD_AI_PROVIDER not in {"openai", "groq"}:
            return "unsupported_provider"
        key = settings.GROQ_API_KEY if settings.BRANDGUARD_AI_PROVIDER == "groq" else settings.OPENAI_API_KEY
        return "real" if key else "missing_key"
    return "disabled"


def ai_unavailable_reason():
    mode = ai_analysis_mode()
    if mode == "conflict":
        return "AI cannot run because real-provider and Demo AI modes are both enabled. Enable only one mode."
    if mode == "disabled":
        return "AI analysis is disabled for this deployment. Deterministic policy review remains available."
    if mode == "missing_key":
        return "AI analysis is unavailable because no provider key is configured. Deterministic policy review remains available."
    if mode == "unsupported_provider":
        return "AI analysis is unavailable because the configured provider is not supported. Deterministic policy review remains available."
    return ""


def current_analysis_is_stale(run, submission, current_policy):
    return bool(
        run.content_hash != run.content_version.content_hash
        or run.content_version_id != submission.current_version_id
        or run.policy_revision != current_policy["revision"]
        or run.policy_snapshot_hash != current_policy["hash"]
        or run.policy_snapshot != current_policy
    )


def run_ai_analysis(*, submission_id, version_number, workspace, actor, idempotency_key, consent):
    """Capture a policy snapshot, call the provider once outside locks, then persist validated results."""
    if not consent:
        raise ValidationError("Confirm the external-processing disclosure before running AI analysis.")
    mode = ai_analysis_mode()
    if mode == "conflict":
        raise AIAnalysisRejected(
            "MODE_CONFLICT",
            "Real-provider AI and Demo AI mode cannot be enabled together. Enable only one mode.",
        )
    if mode == "unsupported_provider":
        raise AIAnalysisRejected("UNSUPPORTED_PROVIDER", ai_unavailable_reason())
    configured_provider = settings.BRANDGUARD_AI_PROVIDER
    provider_name = "demo" if mode == "demo" else configured_provider if configured_provider in {"openai", "groq"} else "unsupported"
    model_name = (
        "simulated-brandguard-demo" if mode == "demo"
        else settings.BRANDGUARD_GROQ_MODEL if provider_name == "groq"
        else settings.BRANDGUARD_AI_MODEL
    )
    try:
        request_key = uuid.UUID(str(idempotency_key))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError("Refresh the content version page and try again.") from exc

    # Lock the workspace row used by source publication. Do not hold a DB transaction
    # open during the network call below.
    with transaction.atomic():
        locked_workspace = Workspace.objects.select_for_update().get(pk=workspace.pk, is_active=True)
        membership = WorkspaceMembership.objects.filter(
            workspace=locked_workspace, user=actor, is_active=True, user__is_active=True
        ).first()
        if membership is None:
            raise PermissionDenied("An active workspace membership is required.")
        submission = ContentSubmission.objects.select_for_update().filter(
            pk=submission_id, workspace_id=locked_workspace.pk
        ).first()
        if submission is None:
            raise ContentSubmission.DoesNotExist
        if membership.role != WorkspaceMembership.Role.CREATOR or submission.creator_id != actor.pk:
            raise PermissionDenied("Only the creator may request AI analysis for their own content.")
        version = ContentVersion.objects.filter(submission=submission, version_number=version_number).first()
        if version is None:
            raise ContentVersion.DoesNotExist
        if len(version.content_text) > MAX_CONTENT_CHARS:
            raise ValidationError("AI analysis is limited to 20,000 characters of submitted content.")

        prior_request = AIAnalysisRun.objects.filter(workspace=locked_workspace, idempotency_key=request_key).first()
        if prior_request:
            if prior_request.content_version_id != version.pk or prior_request.requested_by_id != actor.pk:
                raise ValidationError("This analysis request token was already used for another version.")
            return prior_request

        snapshot = policy_snapshot(locked_workspace)
        prior_complete = AIAnalysisRun.objects.filter(
            workspace=locked_workspace,
            content_version=version,
            content_hash=version.content_hash,
            policy_revision=snapshot["revision"],
            policy_snapshot_hash=snapshot["hash"],
            provider=provider_name,
            model_identifier=model_name,
            status=AIAnalysisRun.Status.COMPLETE,
        ).first()
        if prior_complete:
            return prior_complete

        # Different browser tabs can carry different idempotency tokens. Because
        # every request serializes this creation step on the workspace row, an
        # equivalent in-flight request is visible here before a second paid
        # provider call is started.
        prior_running = AIAnalysisRun.objects.filter(
            workspace=locked_workspace,
            content_version=version,
            content_hash=version.content_hash,
            policy_revision=snapshot["revision"],
            policy_snapshot_hash=snapshot["hash"],
            provider=provider_name,
            model_identifier=model_name,
            status=AIAnalysisRun.Status.RUNNING,
            # The HTTP request is synchronous and bounded. Ignore abandoned
            # rows after that request could reasonably have finished so a
            # killed worker does not block retries forever.
            created_at__gte=timezone.now() - timedelta(
                seconds=settings.BRANDGUARD_AI_TIMEOUT_SECONDS + 30
            ),
        ).order_by("created_at", "pk").first()
        if prior_running:
            return prior_running

        recent_count = AIAnalysisRun.objects.filter(
            requested_by=actor,
            content_version=version,
            created_at__gte=timezone.now() - timedelta(hours=1),
        ).count()
        if recent_count >= MAX_ANALYSES_PER_USER_VERSION_PER_HOUR:
            raise AIAnalysisRejected(
                AIAnalysisRun.ErrorCategory.RATE_LIMITED,
                "The analysis limit for this content version has been reached. Try again later.",
            )

        policy_versions = [
            source_version for source_version in active_policy_versions(locked_workspace)
            if source_version.effective_date is None or source_version.effective_date <= timezone.localdate()
        ]
        context_manifest = _retrieve_context(policy_versions, version.content_text, submission)
        run = AIAnalysisRun.objects.create(
            workspace=locked_workspace,
            content_version=version,
            requested_by=actor,
            content_hash=version.content_hash,
            policy_revision=snapshot["revision"],
            policy_snapshot_hash=snapshot["hash"],
            policy_snapshot=snapshot,
            context_manifest=context_manifest if mode in {"demo", "real"} else [],
            provider=provider_name,
            model_identifier=model_name,
            status=AIAnalysisRun.Status.RUNNING,
            external_processing_consented=mode == "real",
            consented_at=timezone.now() if mode == "real" else None,
            idempotency_key=request_key,
        )
        if mode in {"disabled", "missing_key"}:
            category = (
                AIAnalysisRun.ErrorCategory.AI_DISABLED
                if mode == "disabled" else AIAnalysisRun.ErrorCategory.MISSING_API_KEY
            )
            _finish_failed(run, category)
            return run
        if not context_manifest:
            _finish_failed(run, AIAnalysisRun.ErrorCategory.NO_APPROVED_CONTEXT)
            return run
        payload = {
            "content": version.content_text,
            "content_version_id": str(version.pk),
            "channel": submission.get_channel_display(),
            "audience": submission.audience,
            "campaign_purpose": submission.campaign_purpose,
            "max_findings": settings.BRANDGUARD_AI_MAX_FINDINGS,
            "approved_source_passages": context_manifest,
        }

    response_data = {}
    try:
        if mode == "demo":
            # This repository-controlled adapter does not import or call the OpenAI SDK.
            from .demo import request_demo_analysis
            response_data = request_demo_analysis(payload)
        else:
            response_data = request_analysis(payload, provider=provider_name, model=model_name)
        validated = validate_provider_findings(
            response_data.get("data"),
            content_text=version.content_text,
            workspace_id=workspace.pk,
            snapshot=snapshot,
            context_manifest=context_manifest,
        )
    except AIAnalysisRejected as exc:
        diagnostics = response_data.get("diagnostics", {}) if isinstance(response_data, dict) else {}
        diagnostics = dict(diagnostics) if isinstance(diagnostics, dict) else {}
        if exc.validation_errors:
            diagnostics["validation_errors"] = exc.validation_errors
        if exc.validation_error_count:
            diagnostics["validation_error_count"] = exc.validation_error_count
        parser_stage = exc.parser_stage
        if parser_stage is None and exc.category == AIAnalysisRun.ErrorCategory.INVALID_EVIDENCE:
            parser_stage = "evidence_validation"
        elif parser_stage is None and exc.category == AIAnalysisRun.ErrorCategory.INVALID_RESPONSE:
            parser_stage = "domain_semantic"
        _finish_failed(run, exc.category, diagnostics=diagnostics, parser_stage=parser_stage)
        return run
    except ProviderFailure as exc:
        _finish_failed(run, exc.category, diagnostics=exc.diagnostics)
        return run
    except Exception as exc:  # provider SDK, payload and unexpected failures are not client details
        logger.warning(
            "AI advisory analysis failed (analysis_id=%s, workspace_id=%s, error_type=%s).",
            run.pk,
            run.workspace_id,
            type(exc).__name__,
        )
        _finish_failed(run, AIAnalysisRun.ErrorCategory.PROVIDER_UNAVAILABLE)
        return run

    try:
        with transaction.atomic():
            locked_run = AIAnalysisRun.objects.select_for_update().get(pk=run.pk)
            for item in validated:
                AIAnalysisFinding.objects.create(analysis_run=locked_run, **item)
            locked_run.input_tokens = _bounded_usage(response_data.get("input_tokens"))
            locked_run.output_tokens = _bounded_usage(response_data.get("output_tokens"))
            locked_run.status = AIAnalysisRun.Status.COMPLETE
            locked_run.completed_at = timezone.now()
            locked_run.save(update_fields=["input_tokens", "output_tokens", "status", "completed_at"])
            return locked_run
    except Exception as exc:
        logger.warning(
            "Validated AI analysis could not be persisted (analysis_id=%s, workspace_id=%s, error_type=%s).",
            run.pk,
            run.workspace_id,
            type(exc).__name__,
        )
        _finish_failed(run, AIAnalysisRun.ErrorCategory.PERSISTENCE)
        return run


def validate_provider_findings(data, *, content_text, workspace_id, snapshot, context_manifest):
    """Validate provider structure, parse domain values, then enforce semantics/evidence."""
    _validate_provider_schema_contract(data)
    if len(data["findings"]) > settings.BRANDGUARD_AI_MAX_FINDINGS:
        raise AIAnalysisRejected(
            AIAnalysisRun.ErrorCategory.INVALID_RESPONSE,
            "The provider returned more findings than this analysis permits.",
            parser_stage="domain_semantic",
            validation_errors=[{"path": "findings", "code": "too_many_findings"}],
        )

    snapshot_versions = {
        item.get("version_id"): item for item in (snapshot.get("versions") or []) if isinstance(item, dict)
    }
    supplied = {}
    for item in context_manifest:
        if isinstance(item, dict):
            supplied.setdefault(item.get("source_version_id"), []).append(item)

    valid = []
    for finding_index, raw in enumerate(data["findings"]):
        raw = _parse_provider_finding(raw, finding_index)
        category = raw["category"]
        classification = raw["classification"]
        explanation = raw["explanation"].strip()
        matched_text = raw["matched_text"]
        source_version_id = raw["source_version_id"].strip()
        evidence_excerpt = raw["evidence_excerpt"]
        suggested_wording = raw["suggested_wording"].strip()
        if not explanation or len(explanation) > 1600 or len(matched_text) > 800 or len(evidence_excerpt) > 2000 or len(suggested_wording) > 500:
            field = "explanation" if not explanation or len(explanation) > 1600 else (
                "matched_text" if len(matched_text) > 800 else (
                    "evidence_excerpt" if len(evidence_excerpt) > 2000 else "suggested_wording"
                )
            )
            raise AIAnalysisRejected(
                AIAnalysisRun.ErrorCategory.INVALID_RESPONSE,
                "A finding exceeded the validated text limits.",
                parser_stage="domain_semantic",
                validation_errors=[{"path": f"findings.{finding_index}.{field}", "code": "empty_value" if field == "explanation" and not explanation else "too_long"}],
            )
        if category in {AIAnalysisFinding.Category.TONE, AIAnalysisFinding.Category.MESSAGING} and classification not in {
            AIAnalysisFinding.Classification.POTENTIAL_CONFLICT,
            AIAnalysisFinding.Classification.NEEDS_HUMAN_REVIEW,
        }:
            raise AIAnalysisRejected(
                AIAnalysisRun.ErrorCategory.INVALID_RESPONSE,
                "Tone and messaging advice must be labeled as subjective potential conflicts or human review.",
                parser_stage="domain_semantic",
                validation_errors=[{"path": f"findings.{finding_index}.classification", "code": "unsupported_combination"}],
            )

        source = None
        source_hash = ""
        if source_version_id:
            pin = snapshot_versions.get(source_version_id)
            passages = supplied.get(source_version_id, [])
            if not pin or not passages:
                raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_EVIDENCE, "A finding cited a source version that was not supplied for this analysis.", parser_stage="evidence_validation", validation_errors=[{"path": f"findings.{finding_index}.source_version_id", "code": "invalid_citation"}])
            try:
                source = SourceVersion.objects.select_related("source").get(
                    pk=source_version_id,
                    source__workspace_id=workspace_id,
                    # A source may be superseded while the provider request is in flight.
                    # Its immutable text remains valid evidence for the policy snapshot
                    # captured when this analysis began.
                    status__in=[SourceVersion.Status.APPROVED, SourceVersion.Status.SUPERSEDED],
                )
            except (SourceVersion.DoesNotExist, ValueError, TypeError) as exc:
                raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_EVIDENCE, "A finding cited an unavailable approved source.", parser_stage="evidence_validation", validation_errors=[{"path": f"findings.{finding_index}.source_version_id", "code": "invalid_citation"}]) from exc
            if pin.get("content_hash") != source.content_hash or evidence_excerpt not in source.source_text:
                raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_EVIDENCE, "A finding's cited evidence did not match its pinned approved source.", parser_stage="evidence_validation", validation_errors=[{"path": f"findings.{finding_index}.evidence_excerpt", "code": "invalid_citation"}])
            if not any(evidence_excerpt in passage.get("excerpt", "") for passage in passages):
                raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_EVIDENCE, "A finding cited evidence that was not included in the provider context.", parser_stage="evidence_validation", validation_errors=[{"path": f"findings.{finding_index}.evidence_excerpt", "code": "invalid_citation"}])
            source_hash = source.content_hash
        else:
            permitted_unverified = category == AIAnalysisFinding.Category.CLAIM and classification in {
                AIAnalysisFinding.Classification.NOT_VERIFIED,
                AIAnalysisFinding.Classification.NEEDS_HUMAN_REVIEW,
            }
            if evidence_excerpt or not permitted_unverified:
                raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_EVIDENCE, "This finding requires a valid approved source citation.", parser_stage="evidence_validation", validation_errors=[{"path": f"findings.{finding_index}.source_version_id", "code": "invalid_citation"}])

        text_start = text_end = None
        location_status = "UNAVAILABLE"
        if matched_text:
            text_start = content_text.find(matched_text)
            if text_start < 0:
                raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_RESPONSE, "A matched passage was not found in the analyzed content.", parser_stage="domain_semantic", validation_errors=[{"path": f"findings.{finding_index}.matched_text", "code": "unmatched_content"}])
            text_end = text_start + len(matched_text)
            if content_text[text_start:text_end] != matched_text:
                raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_RESPONSE, "A matched passage did not agree with the analyzed content.", parser_stage="domain_semantic", validation_errors=[{"path": f"findings.{finding_index}.matched_text", "code": "unmatched_content"}])
            location_status = "AVAILABLE"

        if suggested_wording:
            if source is None:
                raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_EVIDENCE, "Suggested wording requires an approved cited source.", parser_stage="evidence_validation", validation_errors=[{"path": f"findings.{finding_index}.suggested_wording", "code": "invalid_citation"}])
            cited_numbers = set(re.findall(r"\d+(?:[.,]\d+)?%?", evidence_excerpt))
            suggestion_numbers = set(re.findall(r"\d+(?:[.,]\d+)?%?", suggested_wording))
            if not suggestion_numbers.issubset(cited_numbers):
                raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_RESPONSE, "Suggested wording introduced a number that is not present in its cited approved evidence.", parser_stage="domain_semantic", validation_errors=[{"path": f"findings.{finding_index}.suggested_wording", "code": "unsupported_numeric_claim"}])

        valid.append({
            "category": category,
            "classification": classification,
            "explanation": explanation,
            "matched_text": matched_text if location_status == "AVAILABLE" else "",
            "location_status": location_status,
            "text_start": text_start,
            "text_end": text_end,
            "source_version": source,
            "source_content_hash": source_hash,
            "evidence_excerpt": evidence_excerpt if source else "",
            "suggested_wording": suggested_wording,
        })
    return valid


def _validate_provider_schema_contract(data):
    """Recheck the exact shared provider schema locally without retaining values."""
    errors = []
    error_count = 0

    def add(path, code):
        nonlocal error_count
        error_count += 1
        if len(errors) < 8:
            errors.append({"path": path, "code": code})

    def visit(value, schema, path):
        kind = schema.get("type")
        if kind == "object":
            if not isinstance(value, dict):
                add(path, "wrong_type")
                return
            properties = schema.get("properties", {})
            required = schema.get("required", [])
            for key in required:
                if key not in value:
                    add(f"{path}.{key}", "missing_key")
            for key in value:
                if key not in properties:
                    add(f"{path}.unknown", "unexpected_key")
            for key, child in properties.items():
                if key in value:
                    visit(value[key], child, f"{path}.{key}")
        elif kind == "array":
            if not isinstance(value, list):
                add(path, "wrong_type")
                return
            for index, item in enumerate(value):
                visit(item, schema["items"], f"{path}.{index}")
        elif kind == "string":
            if not isinstance(value, str):
                add(path, "wrong_type")
            elif "enum" in schema and value not in schema["enum"]:
                add(path, "invalid_enum")

    visit(data, FINDING_SCHEMA, "$")
    if error_count:
        raise AIAnalysisRejected(
            AIAnalysisRun.ErrorCategory.INVALID_RESPONSE,
            "The provider response did not match the required structured format.",
            parser_stage="provider_schema_contract",
            validation_errors=errors,
            validation_error_count=error_count,
        )


def _parse_provider_finding(raw, finding_index):
    """Convert schema-shaped values to normalized domain values without exposing them."""
    try:
        category = AIAnalysisFinding.Category(raw["category"]).value
        classification = AIAnalysisFinding.Classification(raw["classification"]).value
    except ValueError as exc:
        # The shared provider schema uses these same model choice values. This is
        # defensive in case runtime/model configuration differs from import-time schema.
        field = "category" if raw.get("category") not in AIAnalysisFinding.Category.values else "classification"
        raise AIAnalysisRejected(
            AIAnalysisRun.ErrorCategory.INVALID_RESPONSE,
            "A finding used a category or classification BrandGuard cannot parse.",
            parser_stage="domain_parse",
            validation_errors=[{"path": f"findings.{finding_index}.{field}", "code": "invalid_enum"}],
        ) from exc

    # Keep this as a string at the parse boundary because that is the exact
    # provider schema type. Identifier syntax and policy membership are checked
    # with citation identity below, where malformed IDs are rejected as evidence.
    source_version_id = raw["source_version_id"].strip()

    return {
        **raw,
        "category": category,
        "classification": classification,
        "source_version_id": source_version_id,
    }


def _retrieve_context(source_versions, content_text, submission):
    """Bounded lexical retrieval plus priority for explicitly authored voice-rule evidence."""
    terms = _tokens(" ".join((content_text, submission.audience, submission.campaign_purpose)))
    candidates = []
    seen = set()
    for version in source_versions:
        voice_evidence = set(
            version.rules.filter(category=BrandRule.Category.VOICE).values_list("evidence_excerpt", flat=True)
        )
        for passage in _passages(version.source_text):
            key = (str(version.pk), passage)
            if key in seen:
                continue
            seen.add(key)
            overlap = len(terms.intersection(_tokens(passage)))
            priority = 5 if passage in voice_evidence else 0
            candidates.append((priority + overlap, overlap, str(version.pk), version, passage))
    candidates.sort(key=lambda item: (-item[0], -item[1], item[2], item[4]))
    selected = []
    used = 0
    for _, _, _, version, passage in candidates:
        if used + len(passage) + 1 > settings.BRANDGUARD_AI_MAX_CONTEXT_CHARS:
            continue
        selected.append({
            "source_version_id": str(version.pk),
            "source_id": str(version.source_id),
            "source_title": version.source.title,
            "version_number": version.version_number,
            "excerpt": passage,
        })
        used += len(passage) + 1
        if used >= settings.BRANDGUARD_AI_MAX_CONTEXT_CHARS:
            break
    return selected


def _passages(text):
    for paragraph in re.split(r"\n+", text):
        paragraph = paragraph.strip()
        while paragraph:
            if len(paragraph) <= 900:
                yield paragraph
                break
            split = paragraph.rfind(" ", 0, 900)
            split = split if split > 200 else 900
            excerpt = paragraph[:split].strip()
            if excerpt:
                yield excerpt
            paragraph = paragraph[split:].strip()


def _tokens(text):
    return {token.lower() for token in _WORD_RE.findall(text) if len(token) > 2 and token.lower() not in _STOP_WORDS}


def _bounded_usage(value):
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000_000:
        return value
    return None


def _safe_error_metadata(metadata, *, provider, model, parser_stage=None):
    """Persist only bounded, non-content diagnostics useful for provider debugging."""
    metadata = metadata if isinstance(metadata, dict) else {}
    raw_stage = parser_stage or metadata.get("parser_stage", "")
    result = {
        "provider": provider if provider in {"openai", "groq", "demo"} else "unknown",
        "model": re.sub(r"[^A-Za-z0-9._/-]", "", str(model or ""))[:64],
    }
    if isinstance(raw_stage, str) and raw_stage in _SAFE_ERROR_STAGES:
        result["parser_stage"] = raw_stage
    status = metadata.get("response_status")
    if isinstance(status, str) and status in _SAFE_RESPONSE_STATUSES:
        result["response_status"] = status
    item_types = metadata.get("output_item_types")
    if isinstance(item_types, list):
        result["output_item_types"] = [
            item if isinstance(item, str) and item in _SAFE_OUTPUT_TYPES else "other"
            for item in item_types[:8]
        ]
    for key in ("output_text_present", "output_incomplete"):
        if isinstance(metadata.get(key), bool):
            result[key] = metadata[key]
    reason = metadata.get("incomplete_reason")
    if isinstance(reason, str) and reason in _SAFE_INCOMPLETE_REASONS:
        result["incomplete_reason"] = reason
    code = metadata.get("provider_status_code")
    if isinstance(code, int) and not isinstance(code, bool) and 400 <= code <= 599:
        result["provider_status_code"] = code
    finish_reason = metadata.get("finish_reason")
    if isinstance(finish_reason, str) and finish_reason in _SAFE_FINISH_REASONS:
        result["finish_reason"] = finish_reason
    error_type = metadata.get("provider_error_type")
    if isinstance(error_type, str):
        result["provider_error_type"] = re.sub(r"[^A-Za-z0-9_.-]", "", error_type)[:64]
    provider_message = metadata.get("provider_message")
    if isinstance(provider_message, str):
        provider_message = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", provider_message)
        provider_message = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", provider_message)
        provider_message = re.sub(r"\b(?:gsk|sk)-?[A-Za-z0-9_-]{16,}\b", "[redacted credential]", provider_message)
        provider_message = re.sub(r"\s+", " ", provider_message).strip()[:400]
        if provider_message:
            result["provider_message"] = provider_message
    validation_errors = metadata.get("validation_errors")
    if isinstance(validation_errors, list):
        safe_errors = []
        for item in validation_errors[:8]:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            code = item.get("code")
            if (
                isinstance(path, str)
                and re.fullmatch(r"(?:\$|findings)(?:\.\d{1,3}|\.[a-z_]+|\.unknown)*", path)
                and isinstance(code, str)
                and code in _SAFE_VALIDATION_CODES
            ):
                safe_errors.append({"path": path[:96], "code": code})
        if safe_errors:
            result["validation_errors"] = safe_errors
    error_count = metadata.get("validation_error_count")
    if isinstance(error_count, int) and not isinstance(error_count, bool) and 0 < error_count <= 1000:
        result["validation_error_count"] = error_count
    return result


def _finish_failed(run, category, *, diagnostics=None, parser_stage=None):
    run.status = AIAnalysisRun.Status.FAILED
    run.error_category = category
    run.error_metadata = _safe_error_metadata(
        diagnostics, provider=run.provider, model=run.model_identifier, parser_stage=parser_stage,
    )
    run.completed_at = timezone.now()
    run.save(update_fields=["status", "error_category", "error_metadata", "completed_at"])
