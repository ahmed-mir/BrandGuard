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


class AIAnalysisRejected(Exception):
    """Safe, user-displayable analysis rejection with a persisted error category."""

    def __init__(self, category, message):
        self.category = category
        self.public_message = message
        super().__init__(message)


def ai_analysis_available():
    return bool(settings.BRANDGUARD_AI_ENABLED and settings.OPENAI_API_KEY)


def ai_unavailable_reason():
    if not settings.BRANDGUARD_AI_ENABLED:
        return "AI analysis is disabled for this deployment. Deterministic policy review remains available."
    if not settings.OPENAI_API_KEY:
        return "AI analysis is unavailable because no provider key is configured. Deterministic policy review remains available."
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
            provider="openai",
            model_identifier=settings.BRANDGUARD_AI_MODEL,
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
            provider="openai",
            model_identifier=settings.BRANDGUARD_AI_MODEL,
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
            context_manifest=context_manifest if ai_analysis_available() else [],
            provider="openai",
            model_identifier=settings.BRANDGUARD_AI_MODEL,
            status=AIAnalysisRun.Status.RUNNING,
            external_processing_consented=True,
            consented_at=timezone.now(),
            idempotency_key=request_key,
        )
        if not ai_analysis_available():
            category = (
                AIAnalysisRun.ErrorCategory.AI_DISABLED
                if not settings.BRANDGUARD_AI_ENABLED
                else AIAnalysisRun.ErrorCategory.MISSING_API_KEY
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
            "approved_source_passages": context_manifest,
        }

    try:
        response_data = request_analysis(payload)
        validated = validate_provider_findings(
            response_data.get("data"),
            content_text=version.content_text,
            workspace_id=workspace.pk,
            snapshot=snapshot,
            context_manifest=context_manifest,
        )
    except AIAnalysisRejected as exc:
        _finish_failed(run, exc.category)
        return run
    except ProviderFailure as exc:
        _finish_failed(run, exc.category)
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
    """Revalidate schema, citation identity, evidence text and content spans before storage."""
    if not isinstance(data, dict) or set(data) != {"findings"} or not isinstance(data["findings"], list):
        raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_RESPONSE, "The provider response did not match the required schema.")
    if len(data["findings"]) > settings.BRANDGUARD_AI_MAX_FINDINGS:
        raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_RESPONSE, "The provider returned more findings than this analysis permits.")

    snapshot_versions = {
        item.get("version_id"): item for item in (snapshot.get("versions") or []) if isinstance(item, dict)
    }
    supplied = {}
    for item in context_manifest:
        if isinstance(item, dict):
            supplied.setdefault(item.get("source_version_id"), []).append(item)

    valid = []
    required_keys = {
        "category", "classification", "explanation", "matched_text", "source_version_id",
        "evidence_excerpt", "suggested_wording",
    }
    allowed_categories = set(AIAnalysisFinding.Category.values)
    allowed_classes = set(AIAnalysisFinding.Classification.values)
    for raw in data["findings"]:
        if not isinstance(raw, dict) or set(raw) != required_keys:
            raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_RESPONSE, "A finding did not match the required schema.")
        for key in required_keys:
            if not isinstance(raw[key], str):
                raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_RESPONSE, "A finding contained an invalid field type.")
        category = raw["category"]
        classification = raw["classification"]
        explanation = raw["explanation"].strip()
        matched_text = raw["matched_text"]
        source_version_id = raw["source_version_id"].strip()
        evidence_excerpt = raw["evidence_excerpt"]
        suggested_wording = raw["suggested_wording"].strip()
        if category not in allowed_categories or classification not in allowed_classes:
            raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_RESPONSE, "A finding used an unsupported category or classification.")
        if not explanation or len(explanation) > 1600 or len(matched_text) > 800 or len(evidence_excerpt) > 2000 or len(suggested_wording) > 500:
            raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_RESPONSE, "A finding exceeded the validated text limits.")
        if category in {AIAnalysisFinding.Category.TONE, AIAnalysisFinding.Category.MESSAGING} and classification not in {
            AIAnalysisFinding.Classification.POTENTIAL_CONFLICT,
            AIAnalysisFinding.Classification.NEEDS_HUMAN_REVIEW,
        }:
            raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_RESPONSE, "Tone and messaging advice must be labeled as subjective potential conflicts or human review.")

        source = None
        source_hash = ""
        if source_version_id:
            pin = snapshot_versions.get(source_version_id)
            passages = supplied.get(source_version_id, [])
            if not pin or not passages:
                raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_EVIDENCE, "A finding cited a source version that was not supplied for this analysis.")
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
                raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_EVIDENCE, "A finding cited an unavailable approved source.") from exc
            if pin.get("content_hash") != source.content_hash or evidence_excerpt not in source.source_text:
                raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_EVIDENCE, "A finding's cited evidence did not match its pinned approved source.")
            if not any(evidence_excerpt in passage.get("excerpt", "") for passage in passages):
                raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_EVIDENCE, "A finding cited evidence that was not included in the provider context.")
            source_hash = source.content_hash
        else:
            permitted_unverified = category == AIAnalysisFinding.Category.CLAIM and classification in {
                AIAnalysisFinding.Classification.NOT_VERIFIED,
                AIAnalysisFinding.Classification.NEEDS_HUMAN_REVIEW,
            }
            if evidence_excerpt or not permitted_unverified:
                raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_EVIDENCE, "This finding requires a valid approved source citation.")

        text_start = text_end = None
        location_status = "UNAVAILABLE"
        if matched_text:
            text_start = content_text.find(matched_text)
            if text_start < 0:
                raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_RESPONSE, "A matched passage was not found in the analyzed content.")
            text_end = text_start + len(matched_text)
            if content_text[text_start:text_end] != matched_text:
                raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_RESPONSE, "A matched passage did not agree with the analyzed content.")
            location_status = "AVAILABLE"

        if suggested_wording:
            if source is None:
                raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_EVIDENCE, "Suggested wording requires an approved cited source.")
            cited_numbers = set(re.findall(r"\d+(?:[.,]\d+)?%?", evidence_excerpt))
            suggestion_numbers = set(re.findall(r"\d+(?:[.,]\d+)?%?", suggested_wording))
            if not suggestion_numbers.issubset(cited_numbers):
                raise AIAnalysisRejected(AIAnalysisRun.ErrorCategory.INVALID_RESPONSE, "Suggested wording introduced a number that is not present in its cited approved evidence.")

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


def _finish_failed(run, category):
    run.status = AIAnalysisRun.Status.FAILED
    run.error_category = category
    run.completed_at = timezone.now()
    run.save(update_fields=["status", "error_category", "completed_at"])
