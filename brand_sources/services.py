from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from workspaces.models import Workspace

from .models import ApprovedFact, BrandRule, BrandSource, SourceVersion, policy_snapshot_hash


def active_policy_versions(workspace):
    """Query the workspace's currently approved source-version set for a review run."""
    return (
        SourceVersion.objects.filter(source__workspace_id=workspace.pk, status=SourceVersion.Status.APPROVED)
        .select_related("source")
        .order_by("source_id", "version_number")
    )


def policy_snapshot(workspace):
    """Return the persisted revision token and the exact active version identities."""
    versions = active_policy_versions(workspace)
    return {
        "revision": workspace.policy_revision,
        "hash": workspace.policy_snapshot_hash,
        "versions": [
            {"source_id": str(version.source_id), "version_id": str(version.pk), "version_number": version.version_number, "content_hash": version.content_hash}
            for version in versions
        ],
    }


@transaction.atomic
def create_source(*, workspace, owner, title, source_type, description, source_text, effective_date=None, is_demo=False, slug=None):
    source_slug = slug or _unique_source_slug(workspace, title)
    if slug and BrandSource.objects.filter(workspace=workspace, slug=slug).exists():
        raise ValidationError({"title": "A source with this identity already exists in the workspace."})
    source = BrandSource.objects.create(
        workspace=workspace,
        owner=owner,
        title=title,
        slug=source_slug,
        source_type=source_type,
        description=description,
        is_demo=is_demo,
    )
    version = SourceVersion(
        source=source,
        version_number=1,
        source_text=source_text,
        effective_date=effective_date,
        is_demo=is_demo,
    )
    version.full_clean()
    version.save()
    return source, version


@transaction.atomic
def create_draft_version(*, source, workspace):
    """Create one next draft, copying the latest version's complete policy set."""
    locked_source = BrandSource.objects.select_for_update().get(pk=source.pk, workspace=workspace)
    existing = locked_source.versions.filter(status=SourceVersion.Status.DRAFT).first()
    if existing:
        return existing, False

    previous = locked_source.versions.order_by("-version_number").first()
    if previous is None:
        raise ValidationError("This source has no version to copy. Create a source draft first.")

    version = SourceVersion(
        source=locked_source,
        version_number=previous.version_number + 1,
        source_text=previous.source_text,
        effective_date=previous.effective_date,
    )
    version.full_clean()
    version.save()

    fact_map = {}
    for fact in previous.facts.all():
        cloned = ApprovedFact.objects.create(
            source_version=version,
            identifier=fact.identifier,
            subject=fact.subject,
            statement=fact.statement,
            comparison_operator=fact.comparison_operator,
            comparison_value=fact.comparison_value,
            comparison_max=fact.comparison_max,
            comparison_unit=fact.comparison_unit,
            evidence_excerpt=fact.evidence_excerpt,
            evidence_start=fact.evidence_start,
            evidence_end=fact.evidence_end,
            effective_date=fact.effective_date,
        )
        fact_map[fact.identifier] = cloned

    for rule in previous.rules.all():
        BrandRule.objects.create(
            source_version=version,
            approved_fact=fact_map.get(rule.approved_fact.identifier) if rule.approved_fact_id else None,
            identifier=rule.identifier,
            category=rule.category,
            severity=rule.severity,
            description=rule.description,
            rule_type=rule.rule_type,
            matcher_data=rule.matcher_data,
            evidence_excerpt=rule.evidence_excerpt,
            evidence_start=rule.evidence_start,
            evidence_end=rule.evidence_end,
        )
    locked_source.save(update_fields=["updated_at"])
    return version, True


@transaction.atomic
def approve_source_version(*, source_id, version_number, workspace, actor):
    """Publish a validated draft and replace the active source version atomically."""
    locked_workspace = Workspace.objects.select_for_update().get(pk=workspace.pk)
    version = (
        SourceVersion.objects.select_for_update()
        .select_related("source")
        .filter(source__workspace_id=locked_workspace.pk, source_id=source_id, version_number=version_number)
        .first()
    )
    if version is None:
        raise SourceVersion.DoesNotExist
    if version.status != SourceVersion.Status.DRAFT:
        raise ValidationError("Only a draft version can be approved. Earlier approvals remain preserved.")

    version.full_clean()
    for fact in version.facts.all():
        fact.full_clean()
    for rule in version.rules.select_related("approved_fact").all():
        rule.full_clean()

    previous = (
        SourceVersion.objects.select_for_update()
        .filter(source_id=version.source_id, status=SourceVersion.Status.APPROVED)
        .first()
    )
    if previous:
        previous.status = SourceVersion.Status.SUPERSEDED
        previous.save(update_fields=["status"])

    version.status = SourceVersion.Status.APPROVED
    version.approved_by = actor
    version.approved_at = timezone.now()
    version.save(update_fields=["status", "approved_by", "approved_at", "content_hash"])

    locked_workspace.policy_revision += 1
    locked_workspace.policy_snapshot_hash = policy_snapshot_hash(locked_workspace)
    locked_workspace.save(update_fields=["policy_revision", "policy_snapshot_hash"])
    version.source.save(update_fields=["updated_at"])
    return version


def _unique_source_slug(workspace, title):
    from django.utils.text import slugify

    base = slugify(title)[:130].strip("-")
    if not base:
        raise ValidationError({"title": "Use a title that can be represented as a source name."})
    candidate = base
    suffix = 2
    while BrandSource.objects.filter(workspace=workspace, slug=candidate).exists():
        candidate = f"{base[:130 - len(str(suffix)) - 1]}-{suffix}"
        suffix += 1
    return candidate
