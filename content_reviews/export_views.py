"""Protected export endpoints for currently approved submission versions."""
import logging

from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from workspaces.page_context import app_page_context
from workspaces.permissions import workspace_member_required

from .exports import (
    ExportNotFound,
    ExportUnavailable,
    UnsupportedPdfCharacter,
    approved_export_for,
    build_pdf_report,
    safe_export_basename,
)

logger = logging.getLogger(__name__)


def _unavailable(request, reason, status=409):
    context = app_page_context(
        request,
        page_title="Export unavailable",
        active_navigation="reviews",
        extra={"reason": reason},
    )
    return render(
        request,
        "content_reviews/export_unavailable.html",
        context,
        status=status,
    )


def _approved_export(request, submission_id):
    try:
        return approved_export_for(
            submission_id=submission_id,
            user=request.user,
            workspace_id=request.workspace.pk,
        )
    except ExportNotFound as exc:
        raise Http404("This submission is not available for export.") from exc
    except ExportUnavailable as exc:
        return _unavailable(request, str(exc))


def _private_attachment(body, content_type, filename):
    response = HttpResponse(body, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "private, no-store, no-cache, max-age=0, must-revalidate"
    response["Pragma"] = "no-cache"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_GET
@workspace_member_required
def export_approved_text(request, submission_id):
    """Download only the exact content version with a currently valid approval."""
    data = _approved_export(request, submission_id)
    if isinstance(data, HttpResponse):
        return data
    filename = safe_export_basename(data.submission.title, data.version.version_number) + ".txt"
    return _private_attachment(
        data.version.content_text.encode("utf-8"),
        "text/plain; charset=utf-8",
        filename,
    )


@require_GET
@workspace_member_required
def export_approved_pdf(request, submission_id):
    """Generate a private in-memory review report from stored records."""
    data = _approved_export(request, submission_id)
    if isinstance(data, HttpResponse):
        return data
    filename = safe_export_basename(data.submission.title, data.version.version_number) + ".pdf"
    try:
        pdf = build_pdf_report(data)
    except UnsupportedPdfCharacter as exc:
        return _unavailable(request, str(exc), status=422)
    except Exception as exc:
        # Keep document text and renderer internals out of application logs and
        # show a stable message rather than a traceback or a partial download.
        logger.error(
            "PDF report generation failed (submission_id=%s, error_type=%s).",
            data.submission.pk,
            type(exc).__name__,
        )
        return _unavailable(
            request,
            "The PDF report could not be generated safely. Your approval remains unchanged; try the UTF-8 text download or contact an administrator.",
            status=503,
        )
    return _private_attachment(pdf, "application/pdf", filename)
