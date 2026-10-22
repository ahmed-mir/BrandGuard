from django.contrib import messages
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Prefetch
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from workspaces.models import WorkspaceMembership
from workspaces.page_context import app_page_context
from workspaces.permissions import workspace_member_required, workspace_role_required

from .forms import ApprovedFactForm, BrandDocumentImportForm, BrandRuleForm, BrandSourceCreateForm, ImportedTextConfirmationForm, SourceVersionForm
from .ingestion import DocumentImportError
from .models import ApprovedFact, BrandDocumentImport, BrandRule, BrandSource, SourceVersion
from .services import DocumentImportPersistenceError, approve_source_version, confirm_imported_text, create_draft_version, create_source, import_source_document


MANAGER_ROLES = (WorkspaceMembership.Role.BRAND_MANAGER, WorkspaceMembership.Role.ADMIN)


def _is_manager(request):
    return request.workspace_membership.role in MANAGER_ROLES


def _source(request, source_id):
    source = get_object_or_404(
        BrandSource.objects.select_related("owner", "workspace"),
        pk=source_id,
        workspace_id=request.workspace.pk,
    )
    if not _is_manager(request) and not source.versions.filter(status=SourceVersion.Status.APPROVED).exists():
        raise PermissionDenied("This source has not been published for your workspace.")
    return source


def _version(request, source_id, version_number):
    version = get_object_or_404(
        SourceVersion.objects.select_related("source", "source__workspace", "approved_by"),
        source_id=source_id,
        source__workspace_id=request.workspace.pk,
        version_number=version_number,
    )
    if not _is_manager(request) and version.status not in {SourceVersion.Status.APPROVED, SourceVersion.Status.SUPERSEDED}:
        raise PermissionDenied("Draft source versions are visible only to brand managers and workspace admins.")
    return version


def _context(request, page_title, *, extra=None):
    return app_page_context(
        request,
        page_title=page_title,
        active_navigation="brand_sources",
        extra={"can_manage_sources": _is_manager(request), **(extra or {})},
    )


@require_GET
@workspace_member_required
def source_list(request):
    manager = _is_manager(request)
    allowed_statuses = {choice[0].lower() for choice in SourceVersion.Status.choices}
    selected_status = request.GET.get("status", "all" if manager else "approved").lower()
    selected_type = request.GET.get("type", "").upper()
    if not manager:
        selected_status = "approved"
    if selected_status not in allowed_statuses | {"all", "approved"}:
        selected_status = "all" if manager else "approved"
    valid_types = {choice[0] for choice in BrandSource.SourceType.choices}
    if selected_type not in valid_types:
        selected_type = ""

    sources = BrandSource.objects.filter(workspace_id=request.workspace.pk).select_related("owner")
    if not manager:
        sources = sources.filter(versions__status=SourceVersion.Status.APPROVED).distinct()
    elif selected_status != "all":
        sources = sources.filter(versions__status=selected_status.upper()).distinct()
    if selected_type:
        sources = sources.filter(source_type=selected_type)
    sources = sources.prefetch_related(
        Prefetch("versions", queryset=SourceVersion.objects.select_related("approved_by").order_by("-version_number"), to_attr="ordered_versions")
    )
    for source in sources:
        visible = [v for v in source.ordered_versions if manager or v.status in {SourceVersion.Status.APPROVED, SourceVersion.Status.SUPERSEDED}]
        if manager and selected_status != "all":
            matching_status = selected_status.upper()
            matches = [version for version in visible if version.status == matching_status]
            source.list_version = matches[0] if matches else (visible[0] if visible else None)
        else:
            source.list_version = visible[0] if visible else None
        source.list_owner = source.owner.get_full_name().strip() or source.owner.get_username() if source.owner else "Unassigned"

    context = _context(request, "Brand sources", extra={
        "sources": sources,
        "selected_status": selected_status,
        "selected_type": selected_type,
        "source_types": BrandSource.SourceType.choices,
        "source_statuses": [(status, label) for status, label in SourceVersion.Status.choices if manager],
    })
    return render(request, "brand_sources/source_list.html", context)


@require_http_methods(["GET", "POST"])
@workspace_role_required(*MANAGER_ROLES)
def source_create(request):
    form = BrandSourceCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                source, version = create_source(
                    workspace=request.workspace,
                    owner=request.user,
                    title=form.cleaned_data["title"],
                    source_type=form.cleaned_data["source_type"],
                    description=form.cleaned_data["description"],
                    source_text=form.cleaned_data["source_text"],
                    effective_date=form.cleaned_data["effective_date"],
                )
        except ValidationError as exc:
            for error in exc.messages:
                form.add_error(None, error)
        else:
            messages.success(request, "Source created as a draft. Add structured rules and facts before publishing.")
            return redirect("app:brand_sources:version_edit", source_id=source.pk, version_number=version.version_number)
    return render(request, "brand_sources/source_create.html", _context(request, "New brand source", extra={"form": form}))


@require_http_methods(["GET", "POST"])
@workspace_role_required(*MANAGER_ROLES)
def document_import(request):
    form = BrandDocumentImportForm(request.POST or None, request.FILES or None)
    if not settings.PRIVATE_DOCUMENT_IMPORTS_ENABLED:
        return render(request, "brand_sources/document_import.html", _context(
            request,
            "Import a brand document",
            extra={
                "form": form,
                "document_import_disabled_reason": settings.PRIVATE_DOCUMENT_IMPORTS_DISABLED_REASON,
            },
        ))
    if request.method == "POST" and form.is_valid():
        try:
            source, version, document_import_record = import_source_document(
                workspace=request.workspace,
                owner=request.user,
                title=form.cleaned_data["title"],
                source_type=form.cleaned_data["source_type"],
                description=form.cleaned_data["description"],
                upload=form.cleaned_data["document"],
            )
        except DocumentImportError as exc:
            form.add_error("document", str(exc))
        except DocumentImportPersistenceError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Document text extracted. Review and confirm the content before publishing.")
            return redirect("app:brand_sources:version_edit", source_id=source.pk, version_number=version.version_number)
    return render(request, "brand_sources/document_import.html", _context(request, "Import a brand document", extra={"form": form}))


@require_GET
@workspace_member_required
def source_detail(request, source_id):
    source = _source(request, source_id)
    versions = source.versions.select_related("approved_by").order_by("-version_number")
    if not _is_manager(request):
        versions = versions.filter(status__in=[SourceVersion.Status.APPROVED, SourceVersion.Status.SUPERSEDED])
    version_list = list(versions)
    selected = next((version for version in version_list if version.status == SourceVersion.Status.DRAFT), None)
    selected = selected or next((version for version in version_list if version.status == SourceVersion.Status.APPROVED), None)
    selected = selected or (version_list[0] if version_list else None)
    context = _context(request, source.title, extra={"source": source, "versions": version_list, "selected_version": selected})
    return render(request, "brand_sources/source_detail.html", context)


@require_GET
@workspace_member_required
def source_version_detail(request, source_id, version_number):
    source = _source(request, source_id)
    version = _version(request, source_id, version_number)
    facts = version.facts.all()
    rules = version.rules.select_related("approved_fact").all()
    document_import_record = BrandDocumentImport.objects.filter(source_version=version, workspace_id=request.workspace.pk).select_related("uploaded_by", "confirmed_by").first()
    context = _context(request, f"{source.title} · v{version.version_number}", extra={
        "source": source,
        "version": version,
        "versions": source.versions.select_related("approved_by").filter(
            status__in=[SourceVersion.Status.APPROVED, SourceVersion.Status.SUPERSEDED]
        ).order_by("-version_number") if not _is_manager(request) else source.versions.select_related("approved_by").order_by("-version_number"),
        "rules": rules,
        "facts": facts,
        "document_import": document_import_record,
    })
    return render(request, "brand_sources/source_version_detail.html", context)


@require_http_methods(["GET", "POST"])
@workspace_role_required(*MANAGER_ROLES)
def source_version_create(request, source_id):
    source = get_object_or_404(BrandSource, pk=source_id, workspace_id=request.workspace.pk)
    existing = source.versions.filter(status=SourceVersion.Status.DRAFT).first()
    if existing and request.method == "GET":
        messages.info(request, "This source already has an open draft version.")
        return redirect("app:brand_sources:version_edit", source_id=source.pk, version_number=existing.version_number)
    previous = source.versions.order_by("-version_number").first()
    if previous is None:
        raise PermissionDenied("Create the first source version before adding another.")
    form = SourceVersionForm(request.POST or None, instance=SourceVersion(source=source), initial={
        "source_text": previous.source_text,
        "effective_date": previous.effective_date,
    })
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                version, created = create_draft_version(source=source, workspace=request.workspace)
                if not created:
                    messages.info(request, "This source already has an open draft version. The existing draft was kept unchanged.")
                    return redirect("app:brand_sources:version_edit", source_id=source.pk, version_number=version.version_number)
                version.source_text = form.cleaned_data["source_text"]
                version.effective_date = form.cleaned_data["effective_date"]
                version.full_clean()
                version.save()
        except ValidationError as exc:
            for error in exc.messages:
                form.add_error(None, error)
        else:
            messages.success(request, f"Draft version {version.version_number} created. Previous versions were preserved.")
            return redirect("app:brand_sources:version_edit", source_id=source.pk, version_number=version.version_number)
    return render(request, "brand_sources/version_create.html", _context(request, "New draft version", extra={"source": source, "previous": previous, "form": form}))


@require_http_methods(["GET", "POST"])
@workspace_role_required(*MANAGER_ROLES)
def source_version_edit(request, source_id, version_number):
    source = get_object_or_404(BrandSource, pk=source_id, workspace_id=request.workspace.pk)
    version = get_object_or_404(SourceVersion, source=source, version_number=version_number)
    if version.status != SourceVersion.Status.DRAFT:
        raise PermissionDenied("Approved source versions cannot be edited. Create a new draft version.")
    form = SourceVersionForm(request.POST or None, instance=version)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            version = get_object_or_404(
                SourceVersion.objects.select_for_update(),
                source=source,
                version_number=version_number,
            )
            if version.status != SourceVersion.Status.DRAFT:
                raise PermissionDenied("Approved source versions cannot be edited. Create a new draft version.")
            form = SourceVersionForm(request.POST, instance=version)
            if form.is_valid():
                form.save()
                source.save(update_fields=["updated_at"])
                messages.success(request, f"Draft version {version.version_number} saved.")
                return redirect("app:brand_sources:version_edit", source_id=source.pk, version_number=version.version_number)
    return render(request, "brand_sources/version_edit.html", _context(request, f"Edit {source.title} · v{version.version_number}", extra={
        "source": source,
        "version": version,
        "form": form,
        "rules": version.rules.select_related("approved_fact").all(),
        "facts": version.facts.all(),
        "document_import": BrandDocumentImport.objects.filter(source_version=version, workspace_id=request.workspace.pk).select_related("uploaded_by", "confirmed_by").first(),
    }))


@require_POST
@workspace_role_required(*MANAGER_ROLES)
def source_version_confirm_import(request, source_id, version_number):
    form = ImportedTextConfirmationForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Review the saved source text and confirm it before publishing.")
        return redirect("app:brand_sources:version_edit", source_id=source_id, version_number=version_number)
    try:
        confirmed = confirm_imported_text(
            source_id=source_id,
            version_number=version_number,
            workspace=request.workspace,
            actor=request.user,
        )
    except SourceVersion.DoesNotExist:
        raise Http404
    messages.success(request, "Extracted source text confirmed. You can now add policy entries and publish this version.")
    return redirect("app:brand_sources:version_edit", source_id=confirmed.source_version.source_id, version_number=confirmed.source_version.version_number)


@require_POST
@workspace_role_required(*MANAGER_ROLES)
def source_version_approve(request, source_id, version_number):
    try:
        version = approve_source_version(
            source_id=source_id,
            version_number=version_number,
            workspace=request.workspace,
            actor=request.user,
        )
    except SourceVersion.DoesNotExist:
        raise PermissionDenied("The requested draft is not available in this workspace.")
    except ValidationError as exc:
        messages.error(request, "This version could not be published: " + " ".join(exc.messages))
        return redirect("app:brand_sources:version_detail", source_id=source_id, version_number=version_number)
    messages.success(request, f"Version {version.version_number} is now approved and active for this workspace.")
    return redirect("app:brand_sources:version_detail", source_id=source_id, version_number=version_number)


def _policy_version_or_404(request, source_id, version_number):
    version = _version(request, source_id, version_number)
    if version.status != SourceVersion.Status.DRAFT:
        raise PermissionDenied("Policy entries in approved or superseded versions cannot be changed.")
    return version


def _render_entry_form(request, *, source, version, form, kind, title, entry=None):
    return render(request, "brand_sources/entry_form.html", _context(request, title, extra={
        "source": source,
        "version": version,
        "form": form,
        "entry_kind": kind,
        "entry": entry,
        "detail_url": "app:brand_sources:version_detail",
    }))


@require_http_methods(["GET", "POST"])
@workspace_role_required(*MANAGER_ROLES)
def rule_create(request, source_id, version_number):
    version = _policy_version_or_404(request, source_id, version_number)
    form = BrandRuleForm(request.POST or None, source_version=version)
    if request.method == "POST" and form.is_valid():
        try:
            form.save()
        except ValidationError as exc:
            for error in exc.messages:
                form.add_error(None, error)
        else:
            messages.success(request, f"Rule {form.cleaned_data['identifier']} added to this draft.")
            return redirect("app:brand_sources:version_edit", source_id=version.source_id, version_number=version.version_number)
    return _render_entry_form(request, source=version.source, version=version, form=form, kind="rule", title="Add a brand rule")


@require_http_methods(["GET", "POST"])
@workspace_role_required(*MANAGER_ROLES)
def rule_edit(request, source_id, version_number, rule_id):
    version = _policy_version_or_404(request, source_id, version_number)
    rule = get_object_or_404(BrandRule, pk=rule_id, source_version=version)
    form = BrandRuleForm(request.POST or None, instance=rule, source_version=version)
    if request.method == "POST" and form.is_valid():
        try:
            form.save()
        except ValidationError as exc:
            for error in exc.messages:
                form.add_error(None, error)
        else:
            messages.success(request, f"Rule {rule.identifier} updated in this draft.")
            return redirect("app:brand_sources:version_edit", source_id=version.source_id, version_number=version.version_number)
    return _render_entry_form(request, source=version.source, version=version, form=form, kind="rule", title=f"Edit rule {rule.identifier}", entry=rule)


@require_POST
@workspace_role_required(*MANAGER_ROLES)
def rule_delete(request, source_id, version_number, rule_id):
    version = _policy_version_or_404(request, source_id, version_number)
    rule = get_object_or_404(BrandRule, pk=rule_id, source_version=version)
    if rule.approved_fact_id:
        messages.error(request, "This rule represents an approved fact. Remove or update it through the fact record.")
    else:
        identifier = rule.identifier
        rule.delete()
        messages.success(request, f"Rule {identifier} removed from this draft.")
    return redirect("app:brand_sources:version_edit", source_id=version.source_id, version_number=version.version_number)


@require_http_methods(["GET", "POST"])
@workspace_role_required(*MANAGER_ROLES)
def fact_create(request, source_id, version_number):
    version = _policy_version_or_404(request, source_id, version_number)
    form = ApprovedFactForm(request.POST or None, source_version=version)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                form.save()
        except ValidationError as exc:
            for error in exc.messages:
                form.add_error(None, error)
        else:
            messages.success(request, f"Approved fact {form.cleaned_data['identifier']} added with a linked deterministic fact rule.")
            return redirect("app:brand_sources:version_edit", source_id=version.source_id, version_number=version.version_number)
    return _render_entry_form(request, source=version.source, version=version, form=form, kind="fact", title="Add an approved fact")


@require_http_methods(["GET", "POST"])
@workspace_role_required(*MANAGER_ROLES)
def fact_edit(request, source_id, version_number, fact_id):
    version = _policy_version_or_404(request, source_id, version_number)
    fact = get_object_or_404(ApprovedFact, pk=fact_id, source_version=version)
    form = ApprovedFactForm(request.POST or None, instance=fact, source_version=version)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                form.save()
        except ValidationError as exc:
            for error in exc.messages:
                form.add_error(None, error)
        else:
            messages.success(request, f"Fact {fact.identifier} updated in this draft.")
            return redirect("app:brand_sources:version_edit", source_id=version.source_id, version_number=version.version_number)
    return _render_entry_form(request, source=version.source, version=version, form=form, kind="fact", title=f"Edit fact {fact.identifier}", entry=fact)


@require_POST
@workspace_role_required(*MANAGER_ROLES)
def fact_delete(request, source_id, version_number, fact_id):
    version = _policy_version_or_404(request, source_id, version_number)
    fact = get_object_or_404(ApprovedFact, pk=fact_id, source_version=version)
    identifier = fact.identifier
    with transaction.atomic():
        fact.policy_rules.all().delete()
        fact.delete()
    messages.success(request, f"Fact {identifier} and its linked rule were removed from this draft.")
    return redirect("app:brand_sources:version_edit", source_id=version.source_id, version_number=version.version_number)
