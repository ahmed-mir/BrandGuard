"""Bounded deterministic content review against pinned, approved workspace policy."""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import logging
import re
import uuid

from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.db import connection
from django.utils import timezone

from brand_sources.models import BrandRule, SourceVersion, validate_matcher_data
from brand_sources.services import active_policy_versions, policy_snapshot
from workspaces.models import Workspace, WorkspaceMembership

from .models import (
    ContentSubmission,
    ContentVersion,
    ReviewCheck,
    ReviewFinding,
    ReviewPolicyVersion,
    ReviewRun,
)


logger = logging.getLogger(__name__)
ENGINE_VERSION = "deterministic-text-1.0"
MAX_RULES_PER_RUN = 500
MAX_FINDINGS_PER_RUN = 250
SUPPORTED_FACT_UNIT = "%"


@dataclass
class RuleResult:
    outcome: str
    summary: str
    findings: list[dict] = field(default_factory=list)
    incomplete: bool = False


@dataclass
class ReviewStart:
    review_run: ReviewRun
    rules: list[BrandRule]
    pinned_versions: list[SourceVersion]
    existing: bool = False


@transaction.atomic
def create_submission(*, workspace, creator, title, channel, audience, campaign_purpose, content_text, is_demo=False):
    """Persist the first immutable version of a text-only content submission."""
    if not WorkspaceMembership.objects.select_for_update().filter(
        workspace=workspace,
        workspace__is_active=True,
        user=creator,
        user__is_active=True,
        is_active=True,
        role=WorkspaceMembership.Role.CREATOR,
    ).exists():
        raise ValidationError("Only an active creator in this workspace can submit content.")
    if not content_text or not content_text.strip():
        raise ValidationError({"content_text": "Paste the marketing text you want checked."})
    if len(content_text) > 20_000:
        raise ValidationError({"content_text": "Content must be 20,000 characters or fewer."})
    if channel not in ContentSubmission.Channel.values:
        raise ValidationError({"channel": "Choose a supported content channel."})
    with transaction.atomic():
        submission = ContentSubmission.objects.create(
            workspace=workspace,
            creator=creator,
            title=title.strip(),
            channel=channel,
            audience=audience.strip(),
            campaign_purpose=campaign_purpose.strip(),
            is_demo=is_demo,
        )
        version = ContentVersion(
            submission=submission,
            version_number=1,
            content_text=content_text,
            created_by=creator,
            is_demo=is_demo,
        )
        version.full_clean()
        version.save()
        submission.current_version = version
        submission.full_clean()
        submission.save(update_fields=["current_version", "updated_at"])
    return submission, version


def create_revision(*, submission_id, workspace, actor, base_version_id, base_content_hash, content_text, idempotency_key):
    """Append a revision only if the exact base version is still current."""
    try:
        token = uuid.UUID(str(idempotency_key))
        base_id = uuid.UUID(str(base_version_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError("Refresh the version history and try again.") from exc
    if not content_text or not content_text.strip():
        raise ValidationError({"content_text": "Paste the revised marketing text before saving a new version."})
    if len(content_text) > 20_000:
        raise ValidationError({"content_text": "Content must be 20,000 characters or fewer."})

    try:
        with transaction.atomic():
            membership = WorkspaceMembership.objects.select_for_update().filter(
                workspace=workspace,
                workspace__is_active=True,
                user=actor,
                user__is_active=True,
                is_active=True,
                role=WorkspaceMembership.Role.CREATOR,
            ).first()
            if membership is None:
                raise ValidationError("Only an active creator in this workspace can revise content.")
            submission = (
                ContentSubmission.objects.select_for_update()
                .filter(pk=submission_id, workspace=workspace, creator=actor)
                .select_related("current_version")
                .first()
            )
            if submission is None:
                raise ContentSubmission.DoesNotExist

            existing = submission.versions.filter(revision_idempotency_key=token).first()
            if existing:
                submitted_hash = hashlib.sha256(content_text.encode("utf-8")).hexdigest()
                if (
                    existing.revision_of_id == base_id
                    and existing.created_by_id == actor.pk
                    and existing.content_hash == submitted_hash
                ):
                    return submission, existing, False
                raise ValidationError("This revision request was already used for a different version.")

            base = submission.versions.filter(pk=base_id).first()
            if (
                base is None
                or submission.current_version_id != base.pk
                or base.content_hash != base_content_hash
            ):
                raise ValidationError("This content changed in another session. Reload the latest version before revising.")
            if content_text == base.content_text:
                raise ValidationError({"content_text": "Make a change before saving a new content version."})

            next_number = submission.versions.order_by("-version_number").values_list("version_number", flat=True).first() + 1
            version = ContentVersion(
                submission=submission,
                version_number=next_number,
                content_text=content_text,
                created_by=actor,
                revision_of=base,
                revision_idempotency_key=token,
                is_demo=submission.is_demo,
            )
            version.full_clean()
            version.save()
            submission.current_version = version
            submission.full_clean()
            submission.save(update_fields=["current_version", "updated_at"])
            return submission, version, True
    except IntegrityError as exc:
        # The unique version and idempotency constraints are the final guard on
        # databases that cannot serialize the row lock (notably local SQLite).
        raise ValidationError("Another revision was saved first. Reload the latest version and try again.") from exc
    except OperationalError as exc:
        if connection.vendor == "sqlite" and "locked" in str(exc).casefold():
            raise ValidationError("Another revision is being saved. Reload the latest version and try again.") from exc
        raise


def start_review(*, submission_id, version_number, workspace, actor, idempotency_key):
    """Pin the current approved policy set and create a distinct auditable run."""
    try:
        token = uuid.UUID(str(idempotency_key))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError("Refresh the review page and try again.") from exc

    with transaction.atomic():
        locked_workspace = Workspace.objects.select_for_update().get(pk=workspace.pk, is_active=True)
        membership = WorkspaceMembership.objects.select_for_update().filter(
            workspace=locked_workspace,
            user=actor,
            user__is_active=True,
            is_active=True,
            role=WorkspaceMembership.Role.CREATOR,
        ).first()
        if membership is None:
            raise ValidationError("Only an active creator in this workspace can run a content review.")
        submission = (
            ContentSubmission.objects.select_for_update()
            .filter(pk=submission_id, workspace_id=locked_workspace.pk)
            .select_related("creator", "current_version")
            .first()
        )
        if submission is None or submission.creator_id != actor.pk:
            raise ContentSubmission.DoesNotExist
        version = submission.versions.filter(version_number=version_number).first()
        if version is None:
            raise ContentVersion.DoesNotExist

        previous = ReviewRun.objects.filter(workspace=locked_workspace, idempotency_key=token).first()
        if previous:
            if previous.content_version_id != version.pk or previous.requested_by_id != actor.pk:
                raise ValidationError("This review request token was already used for different content.")
            return ReviewStart(previous, [], list(previous.pinned_policy_versions.select_related("source_version")), existing=True)

        snapshot = policy_snapshot(locked_workspace)
        approved_versions = list(active_policy_versions(locked_workspace))
        now = timezone.now()
        review = ReviewRun.objects.create(
            workspace=locked_workspace,
            content_version=version,
            requested_by=actor,
            status=ReviewRun.Status.RUNNING,
            started_at=now,
            policy_revision=snapshot["revision"],
            policy_snapshot_hash=snapshot["hash"],
            policy_snapshot=snapshot,
            engine_version=ENGINE_VERSION,
            idempotency_key=token,
            not_implemented_checks=["tone_analysis"],
        )

        today = timezone.localdate()
        applied_versions = []
        for source_version in approved_versions:
            applies = source_version.effective_date is None or source_version.effective_date <= today
            ReviewPolicyVersion.objects.create(
                review_run=review,
                source_version=source_version,
                content_hash=source_version.content_hash,
                applied=applies,
                exclusion_reason="" if applies else ReviewPolicyVersion.ExclusionReason.FUTURE_EFFECTIVE,
            )
            if applies:
                applied_versions.append(source_version)

        if not approved_versions:
            _finish_failed(
                review,
                code="NO_APPROVED_POLICY",
                message="No approved brand policy is available for this workspace. Ask a brand manager to publish a source first.",
            )
            return ReviewStart(review, [], [], existing=False)
        if not applied_versions:
            _finish_failed(
                review,
                code="NO_EFFECTIVE_POLICY",
                message="Approved sources exist, but none are effective yet. No content checks were run.",
            )
            return ReviewStart(review, [], approved_versions, existing=False)

        applied_ids = [source_version.pk for source_version in applied_versions]
        all_rules = list(
            BrandRule.objects.filter(source_version_id__in=applied_ids)
            .select_related("source_version", "source_version__source", "approved_fact")
            .order_by("source_version_id", "identifier", "id")
        )
        rules = all_rules[:MAX_RULES_PER_RUN]
        if len(all_rules) > len(rules):
            review.incomplete_checks = [{
                "code": "POLICY_RULE_LIMIT",
                "message": f"Only the first {MAX_RULES_PER_RUN} applicable policy rules were evaluated; {len(all_rules) - MAX_RULES_PER_RUN} were not run.",
            }]
            review.save(update_fields=["incomplete_checks"])
        return ReviewStart(review, rules, approved_versions, existing=False)

    # Query scoping above deliberately derives all identity from the locked workspace.


def execute_review(*, submission_id, version_number, workspace, actor, idempotency_key):
    """Run saved rules, preserving every completed check and producing honest terminal state."""
    started = start_review(
        submission_id=submission_id,
        version_number=version_number,
        workspace=workspace,
        actor=actor,
        idempotency_key=idempotency_key,
    )
    review = started.review_run
    if started.existing or review.is_terminal:
        return review

    try:
        # Re-query only by identities pinned while the Workspace row was locked.
        rules = list(
            BrandRule.objects.filter(pk__in=[rule.pk for rule in started.rules])
            .select_related("source_version", "source_version__source", "approved_fact")
            .order_by("source_version_id", "identifier", "id")
        )
        incomplete = list(review.incomplete_checks)
        completed_checks = 0
        finding_count = 0
        for index, rule in enumerate(rules):
            if finding_count >= MAX_FINDINGS_PER_RUN:
                skipped = len(rules) - index
                incomplete.append({
                    "code": "FINDING_LIMIT",
                    "message": f"The run reached the {MAX_FINDINGS_PER_RUN}-finding storage limit; {skipped} remaining rules were not evaluated.",
                })
                for remaining in rules[index:]:
                    _save_check(
                        review,
                        remaining,
                        ReviewCheck.Outcome.NOT_COMPLETED,
                        "Not evaluated because the result limit was reached.",
                        error_code="FINDING_LIMIT",
                    )
                break

            try:
                _validate_rule_evidence(rule, started.pinned_versions)
                result = evaluate_rule(
                    rule,
                    content_text=review.content_version.content_text,
                    channel=review.content_version.submission.channel,
                    finding_limit=MAX_FINDINGS_PER_RUN - finding_count,
                )
                with transaction.atomic():
                    check = _save_check(review, rule, result.outcome, result.summary, finding_count=len(result.findings))
                    for spec in result.findings:
                        ReviewFinding.objects.create(
                            review_run=review,
                            review_check=check,
                            rule=rule,
                            source_version=rule.source_version,
                            origin=ReviewFinding.Origin.DETERMINISTIC,
                            evidence_excerpt=rule.evidence_excerpt,
                            **spec,
                        )
                completed_checks += 1
                finding_count += len(result.findings)
                if result.incomplete:
                    incomplete.append({
                        "code": "RULE_MATCH_LIMIT",
                        "rule_identifier": rule.identifier,
                        "message": f"Additional matches for {rule.identifier} were omitted after the run reached its finding limit.",
                    })
                    skipped = len(rules) - index - 1
                    for remaining in rules[index + 1:]:
                        _save_check(
                            review,
                            remaining,
                            ReviewCheck.Outcome.NOT_COMPLETED,
                            "Not evaluated because the result limit was reached.",
                            error_code="FINDING_LIMIT",
                        )
                    if skipped:
                        incomplete.append({
                            "code": "FINDING_LIMIT",
                            "message": f"{skipped} remaining rules were not evaluated after the finding limit was reached.",
                        })
                    break
            except Exception as exc:
                logger.warning(
                    "A deterministic policy check failed (workspace_id=%s, review_id=%s, rule_id=%s, error_type=%s).",
                    review.workspace_id,
                    review.pk,
                    rule.pk,
                    type(exc).__name__,
                )
                try:
                    _save_check(
                        review,
                        rule,
                        ReviewCheck.Outcome.FAILED,
                        "This policy check could not be completed. Other saved results are preserved.",
                        error_code="CHECK_EXECUTION_FAILED",
                    )
                except Exception:
                    incomplete.append({
                        "code": "CHECK_PERSISTENCE_FAILED",
                        "rule_identifier": rule.identifier,
                        "message": "A policy check result could not be saved.",
                    })
                incomplete.append({
                    "code": "CHECK_EXECUTION_FAILED",
                    "rule_identifier": rule.identifier,
                    "message": "A policy check did not complete.",
                })

        total_checks = review.checks.count()
        failed_checks = review.checks.filter(outcome__in=[ReviewCheck.Outcome.FAILED, ReviewCheck.Outcome.NOT_COMPLETED]).count()
        if incomplete or failed_checks:
            terminal = ReviewRun.Status.PARTIAL if completed_checks or (total_checks > failed_checks) else ReviewRun.Status.FAILED
        else:
            terminal = ReviewRun.Status.COMPLETE
        if not rules and not incomplete:
            # A published policy without structured deterministic rules is not a pass.
            incomplete = []
        review.refresh_from_db()
        review.status = terminal
        review.completed_at = timezone.now()
        review.incomplete_checks = incomplete
        review.save(update_fields=["status", "completed_at", "incomplete_checks"])
    except Exception as exc:
        logger.warning(
            "Review execution failed (workspace_id=%s, review_id=%s, error_type=%s).",
            review.workspace_id,
            review.pk,
            type(exc).__name__,
        )
        review.refresh_from_db()
        if not review.is_terminal:
            review.status = ReviewRun.Status.FAILED
            review.failure_code = "ENGINE_EXECUTION_FAILED"
            review.failure_message = "The review could not be completed. Saved results, if any, remain available. Try a new review run."
            review.completed_at = timezone.now()
            review.incomplete_checks = list(review.incomplete_checks) + [{
                "code": "ENGINE_EXECUTION_FAILED",
                "message": "The review did not finish.",
            }]
            review.save(update_fields=["status", "failure_code", "failure_message", "completed_at", "incomplete_checks"])
    return review


def evaluate_rule(rule, *, content_text, channel, finding_limit):
    """Return a deterministic result for one validated, approved BrandRule."""
    validate_matcher_data(rule.matcher_data)
    matcher = rule.matcher_data
    if rule.rule_type == BrandRule.RuleType.PROHIBITED_TERM:
        if not _scope_applies(matcher.get("scope", "ALL"), channel):
            return RuleResult(ReviewCheck.Outcome.NOT_APPLICABLE, "This rule does not apply to the selected channel.")
        spans, truncated = _find_phrase_spans(
            content_text,
            matcher["term"],
            case_sensitive=matcher.get("case_sensitive", False),
            limit=finding_limit,
        )
        if not spans:
            return RuleResult(ReviewCheck.Outcome.NO_VIOLATION, "The configured prohibited term was not found.")
        findings = [
            _located_finding(
                content_text,
                start,
                end,
                finding_type=ReviewFinding.FindingType.PROHIBITED_TERM,
                classification="",
                explanation=(
                    f"{rule.description} The configured prohibited phrase “{matcher['term']}” appears in this content."
                ),
                suggested_correction=matcher.get("preferred", ""),
            )
            for start, end in spans
        ]
        return RuleResult(ReviewCheck.Outcome.FINDING, f"Found {len(spans)} occurrence(s) of the configured prohibited term.", findings, truncated)

    if rule.rule_type == BrandRule.RuleType.REQUIRED_TERM:
        if not _scope_applies(matcher.get("scope", "ALL"), channel):
            return RuleResult(ReviewCheck.Outcome.NOT_APPLICABLE, "This required-term rule does not apply to the selected channel.")
        term_spans, _ = _find_phrase_spans(
            content_text,
            matcher["term"],
            case_sensitive=matcher.get("case_sensitive", False),
            limit=1,
        )
        if term_spans:
            return RuleResult(ReviewCheck.Outcome.NO_VIOLATION, "The required term is present in the applicable content.")
        trigger = matcher.get("trigger_phrase", "").strip()
        if matcher.get("scope", "ALL") == "ALL" and not trigger:
            return RuleResult(
                ReviewCheck.Outcome.NOT_APPLICABLE,
                "No channel-specific scope or content trigger is configured, so absence is not treated as a violation.",
            )
        if trigger:
            trigger_spans, _ = _find_phrase_spans(content_text, trigger, case_sensitive=False, limit=1)
            if not trigger_spans:
                return RuleResult(ReviewCheck.Outcome.NOT_APPLICABLE, "The configured content trigger was not present.")
        return RuleResult(
            ReviewCheck.Outcome.FINDING,
            "The configured required term was absent in an applicable channel/context.",
            [{
                "finding_type": ReviewFinding.FindingType.REQUIRED_TERM_MISSING,
                "classification": "",
                "location_status": ReviewFinding.LocationStatus.UNAVAILABLE,
                "text_start": None,
                "text_end": None,
                "matched_text": "",
                "explanation": f"{rule.description} Include the configured term “{matcher['term']}” where this policy scope applies.",
                "suggested_correction": matcher["term"],
            }],
        )

    if rule.rule_type == BrandRule.RuleType.REQUIRED_DISCLAIMER:
        if not _scope_applies(matcher.get("scope", "ALL"), channel):
            return RuleResult(ReviewCheck.Outcome.NOT_APPLICABLE, "This disclaimer rule does not apply to the selected channel.")
        phrase = matcher["phrase"]
        phrase_spans, _ = _find_phrase_spans(content_text, phrase, case_sensitive=False, limit=1)
        trigger = matcher.get("trigger_phrase", "").strip()
        if not trigger:
            return RuleResult(
                ReviewCheck.Outcome.NO_VIOLATION if phrase_spans else ReviewCheck.Outcome.NEEDS_HUMAN_REVIEW,
                "The disclaimer is present." if phrase_spans else "No applicability trigger is configured, so a missing disclaimer is not treated as a violation.",
            )
        trigger_spans, _ = _find_phrase_spans(content_text, trigger, case_sensitive=False, limit=1)
        if not trigger_spans:
            return RuleResult(ReviewCheck.Outcome.NOT_APPLICABLE, "The configured claim trigger was not present.")
        if phrase_spans:
            return RuleResult(ReviewCheck.Outcome.NO_VIOLATION, "The required disclaimer is present alongside its configured trigger.")
        return RuleResult(
            ReviewCheck.Outcome.FINDING,
            "The configured trigger is present but the approved disclaimer is missing.",
            [{
                "finding_type": ReviewFinding.FindingType.DISCLAIMER_MISSING,
                "classification": "",
                "location_status": ReviewFinding.LocationStatus.UNAVAILABLE,
                "text_start": None,
                "text_end": None,
                "matched_text": "",
                "explanation": f"{rule.description} The trigger “{trigger}” appears, but the required phrase “{phrase}” does not.",
                "suggested_correction": phrase,
            }],
        )

    if rule.rule_type == BrandRule.RuleType.APPROVED_FACT:
        return _evaluate_approved_fact(rule, content_text=content_text, finding_limit=finding_limit)

    raise ValidationError("This rule type is not supported by the deterministic review engine.")


def _evaluate_approved_fact(rule, *, content_text, finding_limit):
    fact = rule.approved_fact
    if fact is None:
        raise ValidationError("The approved-fact rule has no linked fact record.")
    if fact.effective_date and fact.effective_date > timezone.localdate():
        return RuleResult(
            ReviewCheck.Outcome.NOT_APPLICABLE,
            f"This approved fact becomes effective on {fact.effective_date.isoformat()} and was not applied to this review.",
        )
    metric_phrase = _shared_metric_phrase(fact.subject, fact.statement)
    if not metric_phrase:
        return RuleResult(
            ReviewCheck.Outcome.NEEDS_HUMAN_REVIEW,
            "The approved fact does not define a specific metric phrase that this engine can compare safely.",
        )
    sentence_spans = _sentence_spans(content_text)
    matching = []
    for start, end in sentence_spans:
        sentence = content_text[start:end]
        if _find_phrase_spans(sentence, metric_phrase, case_sensitive=False, limit=1)[0]:
            matching.append((start, end, sentence))
    if not matching:
        return RuleResult(ReviewCheck.Outcome.NOT_APPLICABLE, "No claim matching this approved fact's metric was found.")

    findings = []
    strongest = ReviewCheck.Outcome.NOT_APPLICABLE
    truncated = False
    for sentence_start, sentence_end, sentence in matching:
        numbers = list(re.finditer(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(%|[A-Za-z]+)?", sentence))
        if not numbers:
            continue
        for number_match in numbers:
            raw_value, raw_unit = number_match.groups()
            try:
                claimed_value = Decimal(raw_value)
            except InvalidOperation:
                continue
            classification, explanation = _classify_fact_claim(
                fact,
                sentence=sentence,
                claimed_value=claimed_value,
                claimed_unit=(raw_unit or "").casefold(),
            )
            if classification == ReviewCheck.Outcome.SUPPORTED:
                if strongest in {ReviewCheck.Outcome.NOT_APPLICABLE, ReviewCheck.Outcome.SUPPORTED}:
                    strongest = ReviewCheck.Outcome.SUPPORTED
                continue
            if classification == ReviewCheck.Outcome.CONTRADICTED:
                strongest = ReviewCheck.Outcome.CONTRADICTED
                finding_type = ReviewFinding.FindingType.FACT_CONTRADICTED
            elif classification == ReviewCheck.Outcome.NEEDS_HUMAN_REVIEW:
                if strongest != ReviewCheck.Outcome.CONTRADICTED:
                    strongest = ReviewCheck.Outcome.NEEDS_HUMAN_REVIEW
                finding_type = ReviewFinding.FindingType.FACT_NOT_VERIFIED
            else:
                if strongest not in {ReviewCheck.Outcome.CONTRADICTED, ReviewCheck.Outcome.NEEDS_HUMAN_REVIEW}:
                    strongest = ReviewCheck.Outcome.NOT_VERIFIED
                finding_type = ReviewFinding.FindingType.FACT_NOT_VERIFIED
            if len(findings) >= finding_limit:
                truncated = True
                break
            findings.append(_located_finding(
                content_text,
                sentence_start,
                sentence_end,
                finding_type=finding_type,
                classification=(
                    ReviewFinding.Classification.CONTRADICTED
                    if classification == ReviewCheck.Outcome.CONTRADICTED
                    else ReviewFinding.Classification.NEEDS_HUMAN_REVIEW
                    if classification == ReviewCheck.Outcome.NEEDS_HUMAN_REVIEW
                    else ReviewFinding.Classification.NOT_VERIFIED
                ),
                explanation=explanation,
                suggested_correction="",
            ))
        if truncated:
            break

    if not findings:
        if strongest == ReviewCheck.Outcome.SUPPORTED:
            return RuleResult(ReviewCheck.Outcome.SUPPORTED, "The submitted claim matches the approved fact's specific metric, quantity and supported comparison.")
        return RuleResult(ReviewCheck.Outcome.NOT_APPLICABLE, "No numeric claim for this metric was found.")
    return RuleResult(strongest, f"Found {len(findings)} claim(s) requiring evidence review.", findings, truncated)


def _classify_fact_claim(fact, *, sentence, claimed_value, claimed_unit):
    approved_unit = fact.comparison_unit.casefold()
    if not approved_unit or not fact.comparison_operator or fact.comparison_value is None:
        return ReviewCheck.Outcome.NEEDS_HUMAN_REVIEW, (
            "This numerical claim relates to an approved fact, but the policy has no complete structured comparison. "
            "It is not classified as false."
        )
    if claimed_unit != approved_unit:
        return ReviewCheck.Outcome.NOT_VERIFIED, (
            f"The claim uses “{claimed_unit or 'no explicit unit'}”, while the approved fact uses “{fact.comparison_unit}”. "
            "These quantities are not comparable, so the claim is not verified or declared false."
        )
    expected_direction = _direction_word(fact.statement)
    claimed_direction = _direction_word(sentence)
    if not expected_direction or claimed_direction != expected_direction:
        return ReviewCheck.Outcome.NEEDS_HUMAN_REVIEW, (
            "The metric and unit are present, but the direction of the claim cannot be matched safely to the approved wording. "
            "Human review is needed; this is not classified as false."
        )
    if re.search(r"\b(?:not|never|no|neither|without|cannot|can't|isn't|aren't|wasn't|weren't|doesn't|didn't)\b", sentence, re.IGNORECASE):
        return ReviewCheck.Outcome.NEEDS_HUMAN_REVIEW, (
            "The sentence contains negating language near a comparable metric claim. This deterministic check cannot interpret its meaning safely; human review is needed."
        )

    # A direct normalized claim is the strongest supported signal. The narrow
    # UP_TO comparison also supports the same maximum and direction when stated
    # with the same 'up to' qualifier.
    if _normalized_phrase(fact.statement) in _normalized_phrase(sentence):
        return ReviewCheck.Outcome.SUPPORTED, "The submitted wording contains the approved fact statement and its configured comparison."

    maximum = fact.comparison_value
    if fact.comparison_operator == "UP_TO":
        if claimed_value > maximum:
            return ReviewCheck.Outcome.CONTRADICTED, (
                f"The claim states {claimed_value:g}{fact.comparison_unit} {claimed_direction}, above the approved up-to limit of "
                f"{maximum:g}{fact.comparison_unit} for this same metric. This conflicts with the saved approved fact; the result is limited to that metric and evidence."
            )
        sentence_words = _tokens(sentence)
        if claimed_value == maximum and _has_token_phrase(sentence_words, ["up", "to"]):
            return ReviewCheck.Outcome.SUPPORTED, "The claim matches the approved maximum, metric, unit and 'up to' qualifier."
        return ReviewCheck.Outcome.NOT_VERIFIED, (
            "The approved source gives an upper bound but does not establish this exact lower or point value. "
            "The claim is not verified by the available evidence; this does not mean it is false."
        )

    if fact.comparison_operator != "UP_TO":
        return ReviewCheck.Outcome.NOT_VERIFIED, (
            "This approved fact uses a comparison operator not currently handled by the deterministic checker. "
            "The claim is not classified as false."
        )
    return ReviewCheck.Outcome.NOT_VERIFIED, (
        "The approved comparison does not establish this exact claim with the same metric, unit and wording. "
        "It is not classified as false."
    )


def _save_check(review, rule, outcome, summary, *, finding_count=0, error_code=""):
    return ReviewCheck.objects.create(
        review_run=review,
        rule=rule,
        source_version=rule.source_version,
        outcome=outcome,
        summary=summary,
        finding_count=finding_count,
        error_code=error_code,
    )


def _validate_rule_evidence(rule, pinned_versions):
    pinned_ids = {version.pk for version in pinned_versions if version.effective_date is None or version.effective_date <= timezone.localdate()}
    if rule.source_version_id not in pinned_ids:
        raise ValidationError("The rule's source version is not in this review's applied policy snapshot.")
    if rule.source_version.content_hash != next(
        version.content_hash for version in pinned_versions if version.pk == rule.source_version_id
    ):
        raise ValidationError("The source content hash no longer matches the pinned policy version.")
    if not rule.evidence_excerpt or rule.evidence_excerpt not in rule.source_version.source_text:
        raise ValidationError("The saved rule evidence is missing from its source version.")
    validate_matcher_data(rule.matcher_data)
    if rule.rule_type == BrandRule.RuleType.APPROVED_FACT:
        fact = rule.approved_fact
        if fact is None or fact.source_version_id != rule.source_version_id:
            raise ValidationError("The approved-fact rule has no matching fact in its source version.")
        if not fact.evidence_excerpt or fact.evidence_excerpt not in rule.source_version.source_text:
            raise ValidationError("The approved fact evidence is missing from its source version.")
        if fact.evidence_excerpt != rule.evidence_excerpt:
            raise ValidationError("The approved fact and its deterministic rule do not share the same evidence excerpt.")


def _finish_failed(review, *, code, message):
    review.status = ReviewRun.Status.FAILED
    review.failure_code = code
    review.failure_message = message
    review.completed_at = timezone.now()
    review.save(update_fields=["status", "failure_code", "failure_message", "completed_at"])


def _scope_applies(scope, channel):
    return scope == "ALL" or scope == channel


def _find_phrase_spans(text, phrase, *, case_sensitive=False, limit):
    if not phrase or limit <= 0:
        return [], False
    flags = re.UNICODE | (0 if case_sensitive else re.IGNORECASE)
    pattern = re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)", flags)
    spans = []
    truncated = False
    for match in pattern.finditer(text):
        if len(spans) >= limit:
            truncated = True
            break
        spans.append((match.start(), match.end()))
    return spans, truncated


def _located_finding(content, start, end, *, finding_type, classification, explanation, suggested_correction):
    return {
        "finding_type": finding_type,
        "classification": classification,
        "location_status": ReviewFinding.LocationStatus.AVAILABLE,
        "text_start": start,
        "text_end": end,
        "matched_text": content[start:end],
        "explanation": explanation,
        "suggested_correction": suggested_correction,
    }


def _sentence_spans(text):
    spans = []
    start = 0
    for separator in re.finditer(r"(?<=[.!?])\s+|\n+", text):
        end = separator.start()
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start < end:
            spans.append((start, end))
        start = separator.end()
    end = len(text)
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start < end:
        spans.append((start, end))
    return spans


def _tokens(text):
    return re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE)


def _shared_metric_phrase(subject, statement):
    subject_tokens = _tokens(subject)
    statement_tokens = _tokens(statement)
    best = []
    for subject_index in range(len(subject_tokens)):
        for statement_index in range(len(statement_tokens)):
            length = 0
            while (
                subject_index + length < len(subject_tokens)
                and statement_index + length < len(statement_tokens)
                and subject_tokens[subject_index + length] == statement_tokens[statement_index + length]
            ):
                length += 1
            if length > len(best):
                best = subject_tokens[subject_index:subject_index + length]
    return " ".join(best) if len(best) >= 2 else ""


def _direction_word(text):
    # Deliberately exact; this engine does not infer synonyms such as
    # productivity, efficiency, or completion speed from one another.
    words = set(_tokens(text))
    directions = {"faster", "slower", "higher", "lower", "increased", "decreased", "increase", "decrease"}
    matches = words & directions
    return next(iter(matches)) if len(matches) == 1 else ""


def _normalized_phrase(text):
    return " ".join(_tokens(text))


def _has_token_phrase(tokens, phrase):
    width = len(phrase)
    return any(tokens[index:index + width] == phrase for index in range(len(tokens) - width + 1))
