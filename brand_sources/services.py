import hashlib
import logging
import re
import uuid

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from workspaces.models import Workspace

from .ingestion import DocumentImportError, extract_uploaded_document
from .models import ApprovedFact, BrandDocumentImport, BrandRule, BrandSource, SourceVersion, policy_snapshot_hash


logger = logging.getLogger(__name__)


class DocumentImportPersistenceError(Exception):
    """The file or policy record could not be safely persisted."""


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
def create_source(*, workspace, owner, title, source_type, description, source_text, effective_date=None, is_demo=False, slug=None, idempotency_key=None):
    token = None
    if idempotency_key is not None:
        try:
            token = uuid.UUID(str(idempotency_key))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValidationError("Refresh the source form and try again.") from exc
    locked_workspace = Workspace.objects.select_for_update().get(pk=workspace.pk, is_active=True)
    title = title.strip()
    existing = None
    if token and owner is not None:
        existing = BrandSource.objects.filter(
            workspace=locked_workspace, owner=owner, create_idempotency_key=token,
        ).first()
    if existing:
        version = existing.versions.filter(version_number=1).first()
        if (
            version is not None
            and existing.title == title
            and existing.source_type == source_type
            and existing.description == description
            and existing.is_demo == is_demo
            and version.source_text == source_text
            and version.effective_date == effective_date
            and (slug is None or existing.slug == slug)
        ):
            return existing, version
        raise ValidationError("This source request was already used for different source details.")

    source_slug = slug or _unique_source_slug(locked_workspace, title)
    if slug and BrandSource.objects.filter(workspace=workspace, slug=slug).exists():
        raise ValidationError({"title": "A source with this identity already exists in the workspace."})
    source = BrandSource.objects.create(
        workspace=locked_workspace,
        owner=owner,
        title=title,
        slug=source_slug,
        source_type=source_type,
        description=description,
        is_demo=is_demo,
        create_idempotency_key=token,
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


def import_source_document(*, workspace, owner, title, source_type, description, upload, idempotency_key=None):
    """Extract and persist a private document linked to a real Phase 2A draft."""
    try:
        extracted = extract_uploaded_document(upload)
    except DocumentImportError:
        raise
    except Exception as exc:
        logger.warning(
            "Brand document could not be read (workspace_id=%s, error_type=%s).",
            workspace.pk,
            type(exc).__name__,
        )
        raise DocumentImportError(
            "The selected document could not be read safely. Choose another file or enter the authoritative text manually."
        ) from exc
    safe_filename = _safe_original_filename(upload.name, extracted.document_type)
    file_hash = hashlib.sha256(extracted.raw_bytes).hexdigest()
    try:
        token = uuid.UUID(str(idempotency_key)) if idempotency_key is not None else None
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError("Refresh the document import form and try again.") from exc
    document_import = BrandDocumentImport(
        workspace=workspace,
        original_filename=safe_filename,
        document_type=extracted.document_type,
        uploaded_by=owner,
        file_sha256=file_hash,
        extracted_character_count=len(extracted.text),
        pdf_page_count=extracted.page_count,
    )
    stored_name = ""
    try:
        with transaction.atomic():
            locked_workspace = Workspace.objects.select_for_update().get(pk=workspace.pk, is_active=True)
            if token:
                existing_source = BrandSource.objects.filter(
                    workspace=locked_workspace, owner=owner, create_idempotency_key=token,
                ).first()
                if existing_source:
                    existing_version = existing_source.versions.filter(version_number=1).first()
                    record = BrandDocumentImport.objects.filter(
                        workspace_id=locked_workspace.pk, source_version=existing_version,
                    ).first() if existing_version else None
                    if (
                        record is not None
                        and existing_source.title == title.strip()
                        and existing_source.source_type == source_type
                        and existing_source.description == description
                        and existing_version.source_text == extracted.text
                        and existing_version.effective_date is None
                        and record.original_filename == safe_filename
                        and record.document_type == extracted.document_type
                        and record.file_sha256 == file_hash
                        and record.extracted_character_count == len(extracted.text)
                        and record.pdf_page_count == extracted.page_count
                    ):
                        return existing_source, existing_version, record
                    raise ValidationError("This import request was already used for a different document or source.")
            source, version = create_source(
                workspace=locked_workspace,
                owner=owner,
                title=title,
                source_type=source_type,
                description=description,
                source_text=extracted.text,
                idempotency_key=token,
            )
            document_import.source_version = version
            # upload_to derives only a generated UUID and validated extension;
            # the client filename is stored as display metadata, never as a path.
            generated_filename = f"{uuid.uuid4().hex}.{extracted.document_type.lower()}"
            document_import.original_document.save(
                generated_filename,
                ContentFile(extracted.raw_bytes),
                save=False,
            )
            stored_name = document_import.original_document.name
            document_import.full_clean()
            document_import.save()
    except ValidationError:
        raise
    except Exception as exc:
        if stored_name:
            try:
                document_import.original_document.storage.delete(stored_name)
            except Exception:
                logger.exception("Private brand document cleanup failed after an import error.")
        logger.warning(
            "Brand document import failed before completion (workspace_id=%s, error_type=%s).",
            workspace.pk,
            type(exc).__name__,
        )
        raise DocumentImportPersistenceError(
            "The document could not be stored safely. No brand source was created; please try again."
        ) from exc
    return source, version, document_import


@transaction.atomic
def confirm_imported_text(*, source_id, version_number, workspace, actor, expected_content_hash):
    document_import = (
        BrandDocumentImport.objects.select_for_update()
        .select_related("source_version", "source_version__source")
        .filter(
            workspace_id=workspace.pk,
            source_version__source_id=source_id,
            source_version__source__workspace_id=workspace.pk,
            source_version__version_number=version_number,
            source_version__status=SourceVersion.Status.DRAFT,
        )
        .first()
    )
    if document_import is None:
        raise SourceVersion.DoesNotExist
    if document_import.source_version.content_hash != expected_content_hash:
        raise ValidationError("The source text changed after this page was opened. Review the current saved text before confirming it.")
    if document_import.confirmed_text_sha256 == document_import.source_version.content_hash:
        return document_import
    document_import.confirmed_text_sha256 = document_import.source_version.content_hash
    document_import.confirmed_by = actor
    document_import.confirmed_at = timezone.now()
    document_import.full_clean()
    document_import.save(update_fields=["confirmed_text_sha256", "confirmed_by", "confirmed_at"])
    return document_import


@transaction.atomic
def create_draft_version(*, source, workspace, expected_previous_version=None, expected_previous_hash=None):
    """Create one next draft, copying the latest version's complete policy set."""
    locked_source = BrandSource.objects.select_for_update().get(pk=source.pk, workspace=workspace)
    existing = locked_source.versions.filter(status=SourceVersion.Status.DRAFT).first()
    if existing:
        return existing, False

    previous = locked_source.versions.order_by("-version_number").first()
    if previous is None:
        raise ValidationError("This source has no version to copy. Create a source draft first.")
    if expected_previous_version is not None and (
        previous.version_number != expected_previous_version
        or previous.content_hash != expected_previous_hash
    ):
        raise ValidationError("The source changed after this form was opened. Reload the latest version before creating a draft.")

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

    try:
        document_import = version.document_import
    except BrandDocumentImport.DoesNotExist:
        document_import = None
    if document_import and not document_import.text_is_confirmed:
        raise ValidationError("Review the extracted text and confirm its current version before publishing this imported source.")

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


def _safe_original_filename(value, document_type):
    filename = str(value or "").replace("\\", "/").split("/")[-1]
    filename = re.sub(r"[\x00-\x1f\x7f]", "", filename).strip()
    filename = filename[:255]
    if not filename or filename in {".", ".."}:
        return f"brand-document.{document_type.lower()}"
    return filename
