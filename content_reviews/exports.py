"""Eligibility and rendering services for private, approved content exports."""
from dataclasses import dataclass
from datetime import datetime
import hashlib
from io import BytesIO
from pathlib import Path

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from approvals.services import current_valid_approval
from content_reviews.models import ContentSubmission, ReviewCheck, ReviewRun
from workspaces.models import Workspace, WorkspaceMembership
from workspaces.permissions import resolve_current_membership


class ExportUnavailable(Exception):
    """An export is not permitted because no current approval can be proven."""


class ExportNotFound(Exception):
    """The submission is outside the caller's visible workspace/ownership scope."""


class UnsupportedPdfCharacter(Exception):
    """The configured embedded PDF font cannot represent all source characters."""


@dataclass(frozen=True)
class ApprovedExport:
    submission: ContentSubmission
    version: object
    approval: object
    decision: object
    review: ReviewRun
    checks: tuple
    findings: tuple
    policy_versions: tuple
    generated_at: datetime


def approved_export_for(*, submission_id, user, workspace_id=None):
    """Resolve permission and current validity anew, then materialize report records.

    The workspace lock coordinates with policy publication; the submission lock
    coordinates with creator revisions on databases that support row locks.
    SQLite does not provide the same multi-writer guarantee as PostgreSQL.
    """
    if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        raise PermissionDenied("Sign in to download approved content.")
    membership = resolve_current_membership(user)
    if membership is None or (workspace_id is not None and membership.workspace_id != workspace_id):
        raise PermissionDenied("An active workspace membership is required to export content.")
    if membership.role not in {
        WorkspaceMembership.Role.CREATOR,
        WorkspaceMembership.Role.REVIEWER,
        WorkspaceMembership.Role.BRAND_MANAGER,
        WorkspaceMembership.Role.ADMIN,
    }:
        raise PermissionDenied("Your workspace role cannot export content.")

    with transaction.atomic():
        workspace = Workspace.objects.select_for_update().filter(
            pk=membership.workspace_id, is_active=True
        ).first()
        if workspace is None:
            raise PermissionDenied("The active workspace is unavailable.")
        membership = WorkspaceMembership.objects.select_for_update().filter(
            pk=membership.pk,
            user=user,
            workspace=workspace,
            user__is_active=True,
            is_active=True,
        ).first()
        if membership is None or membership.role not in {
            WorkspaceMembership.Role.CREATOR,
            WorkspaceMembership.Role.REVIEWER,
            WorkspaceMembership.Role.BRAND_MANAGER,
            WorkspaceMembership.Role.ADMIN,
        }:
            raise PermissionDenied("Your active workspace role cannot export content.")
        submission = _locked_export_submission_queryset(
            submission_id=submission_id,
            workspace_id=workspace.pk,
        ).first()
        if submission is None:
            raise ExportNotFound
        if membership.role == WorkspaceMembership.Role.CREATOR and submission.creator_id != user.pk:
            raise ExportNotFound

        approval_state = current_valid_approval(submission)
        if not approval_state["valid"]:
            raise ExportUnavailable(approval_state["reason"])
        approval = approval_state["approval"]
        decision = approval_state["decision"]
        if (
            approval is None
            or decision is None
            or approval.status != "APPROVED"
            or decision.action != "APPROVED"
            or approval.content_version_id != submission.current_version_id
        ):
            raise ExportUnavailable("This submission has no current approved version to export.")

        review = approval.review_run
        if (
            review.status != ReviewRun.Status.COMPLETE
            or review.workspace_id != workspace.pk
            or review.content_version_id != approval.content_version_id
            or review.checks.filter(outcome__in=[ReviewCheck.Outcome.FAILED, ReviewCheck.Outcome.NOT_COMPLETED]).exists()
        ):
            raise ExportUnavailable("The linked review is not a complete review of this exact version.")

        checks = tuple(review.checks.select_related("rule", "source_version", "source_version__source").order_by("rule__category", "rule__identifier", "id"))
        pins = tuple(
            review.pinned_policy_versions.select_related("source_version", "source_version__source")
            .order_by("source_version__source__title", "source_version__version_number")
        )
        for pin in pins:
            source_version = pin.source_version
            if (
                source_version.source.workspace_id != workspace.pk
                or pin.content_hash != source_version.content_hash
                or hashlib.sha256(source_version.source_text.encode("utf-8")).hexdigest() != pin.content_hash
                or (pin.applied and source_version.status != "APPROVED")
            ):
                raise ExportUnavailable("A pinned policy source no longer matches the exact approved source version.")
        pinned_hashes = {pin.source_version_id: pin.content_hash for pin in pins if pin.applied}
        applied_version_ids = set(pinned_hashes)
        for check in checks:
            if (
                check.source_version_id not in applied_version_ids
                or check.rule.source_version_id != check.source_version_id
                or check.source_version.content_hash != pinned_hashes.get(check.source_version_id)
            ):
                raise ExportUnavailable("A saved review check no longer matches its pinned approved policy evidence.")

        findings = tuple(
            review.findings.select_related(
                "rule", "rule__approved_fact", "source_version", "source_version__source", "review_check"
            ).order_by("rule__severity", "rule__identifier", "id")
        )
        for finding in findings:
            if (
                finding.source_version_id not in applied_version_ids
                or finding.rule.source_version_id != finding.source_version_id
                or finding.review_check.review_run_id != review.pk
                or finding.review_check.rule_id != finding.rule_id
                or finding.evidence_excerpt != finding.rule.evidence_excerpt
                or finding.evidence_excerpt not in finding.source_version.source_text
            ):
                raise ExportUnavailable("A saved finding does not have verifiable evidence in the pinned policy snapshot.")
            if finding.location_status == finding.LocationStatus.AVAILABLE:
                content = approval.content_version.content_text
                if content[finding.text_start:finding.text_end] != finding.matched_text:
                    raise ExportUnavailable("A finding location no longer matches the approved content version.")

        versions = tuple(pin for pin in pins if pin.applied)
        generated_at = timezone.now()
        return ApprovedExport(
            submission=submission,
            version=approval.content_version,
            approval=approval,
            decision=decision,
            review=review,
            checks=checks,
            findings=findings,
            policy_versions=versions,
            generated_at=generated_at,
        )


def _locked_export_submission_queryset(*, submission_id, workspace_id):
    """Lock the submission row without joining its nullable current version."""
    return ContentSubmission.objects.select_for_update().filter(
        pk=submission_id,
        workspace_id=workspace_id,
    ).select_related("workspace", "creator")


def safe_export_basename(title, version_number):
    """Return a deterministic ASCII filename fragment safe for HTTP headers."""
    slug = slugify(title, allow_unicode=False)[:60].strip("-_") or "approved-content"
    return f"brandguard-{slug}-v{int(version_number)}"


def _display_name(user):
    return user.get_full_name().strip() or user.get_username()


def _report_payload(data):
    from brand_sources.models import BrandRule

    severity_counts = {key: 0 for key, _ in BrandRule.Severity.choices}
    for finding in data.findings:
        severity_counts[finding.rule.severity] = severity_counts.get(finding.rule.severity, 0) + 1
    high_ids = {str(item) for item in data.decision.overridden_finding_ids}
    overridden = [finding for finding in data.findings if str(finding.pk) in high_ids]
    return {
        "data": data,
        "reviewer": _display_name(data.decision.reviewer),
        "checks_by_outcome": tuple((value, label, sum(check.outcome == value for check in data.checks)) for value, label in ReviewCheck.Outcome.choices),
        "severity_counts": tuple((label, severity_counts.get(value, 0)) for value, label in BrandRule.Severity.choices),
        "overridden_findings": tuple(overridden),
    }


def build_text_report(data: ApprovedExport) -> bytes:
    """Render exact approved text with a truthful, evidence-backed summary."""
    payload = _report_payload(data)
    counts = ", ".join(f"{label}: {count}" for label, count in payload["severity_counts"])
    lines = [
        "BrandGuard — Approved Content and Review Report",
        f"Submission: {data.submission.title} ({data.submission.pk})",
        f"Workspace: {data.submission.workspace.name}",
        f"Channel: {data.submission.get_channel_display()}",
        f"Content version: {data.version.version_number} ({data.version.pk})",
        f"Content SHA-256: {data.version.content_hash}",
        f"Version created: {timezone.localtime(data.version.created_at).isoformat()}",
        f"Report generated: {timezone.localtime(data.generated_at).isoformat()}",
        "",
        "HUMAN APPROVAL",
        f"Decision: {data.decision.get_action_display()}",
        f"Approval decision ID: {data.decision.pk}",
        f"Reviewer: {payload['reviewer']} ({data.decision.reviewer_id})",
        f"Approved at: {timezone.localtime(data.decision.decided_at).isoformat()}",
        f"Approval request ID: {data.approval.pk}",
        "",
        "REVIEW AND POLICY IDENTITY",
        f"Review run ID: {data.review.pk}",
        f"Review status: {data.review.get_status_display()}",
        f"Engine version: {data.review.engine_version}",
        f"Policy revision: {data.review.policy_revision}",
        f"Policy snapshot SHA-256: {data.review.policy_snapshot_hash}",
        "Approved source versions used:",
    ]
    lines.extend(
        f"- {item.source_version.source.title} · source {item.source_version.source_id} · version {item.source_version.version_number} · version ID {item.source_version_id} · SHA-256 {item.content_hash}"
        for item in data.policy_versions
    )
    if not data.policy_versions:
        lines.append("- No applied source versions were recorded.")
    lines.extend(["", "COMPLETED DETERMINISTIC CHECKS"])
    if data.checks:
        for check in data.checks:
            lines.append(
                f"- {check.rule.identifier} · {check.rule.get_category_display()} · {check.get_outcome_display()} · {check.source_version.source.title} v{check.source_version.version_number} · {check.summary}"
            )
    else:
        lines.append("No structured policy checks were recorded; no compliance conclusion is implied.")
    lines.extend(["", f"Findings by severity: {counts}", f"Outstanding findings: {len(data.findings)} (findings are not dismissed by approval)", "", "FINDINGS AND VERIFIED EVIDENCE"])
    if data.findings:
        for finding in data.findings:
            lines.extend([
                f"{finding.rule.identifier} · {finding.rule.get_category_display()} · {finding.rule.get_severity_display()} severity · {finding.get_finding_type_display()}",
                f"Finding ID: {finding.pk} · Status: {finding.get_resolution_status_display()}",
                f"Source: {finding.source_version.source.title} · source ID {finding.source_version.source_id} · version {finding.source_version.version_number} · version ID {finding.source_version_id}",
            ])
            if finding.matched_text:
                lines.append(f"Matched content: {finding.matched_text}")
            lines.append(f"Explanation: {finding.explanation}")
            if finding.suggested_correction:
                lines.append(f"Suggested wording (not automatically applied): {finding.suggested_correction}")
            lines.extend([f"Verified source evidence: {finding.evidence_excerpt}", ""])
    else:
        lines.append("No finding records were generated by this review run. This is not a universal factual or compliance guarantee.")
    if data.decision.overridden_finding_ids:
        lines.extend(["HIGH-SEVERITY OVERRIDE", "The approval did not dismiss the underlying finding(s)."])
        lines.append(f"Override reason: {data.decision.override_reason}")
        lines.append(f"Decision maker: {payload['reviewer']} ({data.decision.reviewer_id})")
        for finding in payload["overridden_findings"]:
            lines.extend([f"Original finding: {finding.rule.identifier} · finding ID {finding.pk}", f"Original matched content: {finding.matched_text or 'No exact location was recorded.'}", f"Original evidence: {finding.evidence_excerpt}"])
    elif data.decision.override_reason:
        lines.extend(["", "REVIEWER NOTE", data.decision.override_reason])
    not_implemented = data.review.not_implemented_checks or []
    lines.extend(["", "LIMITATIONS", "This report records BrandGuard's completed deterministic checks and a human approval of this exact content version against the recorded policy snapshot.", "It is not legal clearance, universal factual verification, accessibility certification, or tamper-proof proof of approval. No AI analysis was performed."])
    if not_implemented:
        lines.append("Not implemented checks: " + "; ".join(str(item) for item in not_implemented))
    incomplete = data.review.incomplete_checks or []
    if incomplete:
        lines.append("Review records list incomplete checks: " + "; ".join(str(item) for item in incomplete))
    lines.extend(["", "APPROVED CONTENT — EXACT SAVED VERSION", "=" * 72, data.version.content_text])
    return "\n".join(lines).encode("utf-8")


def build_pdf_report(data: ApprovedExport) -> bytes:
    """Build a paginated in-memory PDF; user/source text is plain escaped text."""
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Table, TableStyle

    font_dir = Path(__import__("reportlab").__file__).parent / "fonts"
    regular_path = font_dir / "Vera.ttf"
    bold_path = font_dir / "VeraBd.ttf"
    if not regular_path.is_file() or not bold_path.is_file():
        raise RuntimeError("The embedded ReportLab Vera fonts are unavailable.")
    if "BrandGuardVera" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("BrandGuardVera", str(regular_path)))
        pdfmetrics.registerFont(TTFont("BrandGuardVera-Bold", str(bold_path)))

    payload = _report_payload(data)
    all_text = [
        data.version.content_text,
        data.submission.title,
        data.submission.workspace.name,
        data.decision.override_reason,
        _display_name(data.decision.reviewer),
        data.review.failure_message,
    ]
    for item in data.policy_versions:
        all_text.append(item.source_version.source.title)
    for finding in data.findings:
        all_text.extend([
            finding.rule.identifier,
            finding.rule.get_category_display(),
            finding.rule.get_severity_display(),
            finding.get_finding_type_display(),
            finding.get_resolution_status_display(),
            finding.source_version.source.title,
            finding.matched_text,
            finding.explanation,
            finding.evidence_excerpt,
            finding.suggested_correction,
        ])
    for check in data.checks:
        all_text.extend([
            check.rule.identifier,
            check.get_outcome_display(),
            check.source_version.source.title,
            check.summary,
        ])
    for value in all_text:
        for character in value:
            if character in "\r\n\t" or ord(character) < 32:
                if character not in "\r\n\t":
                    raise UnsupportedPdfCharacter(f"PDF cannot safely render control character U+{ord(character):04X}.")
                continue
            if ord(character) not in pdfmetrics.getFont("BrandGuardVera").face.charWidths:
                raise UnsupportedPdfCharacter(f"PDF font cannot represent U+{ord(character):04X}; download the UTF-8 text export instead.")

    ink = colors.HexColor("#192B2C")
    teal = colors.HexColor("#176F66")
    muted = colors.HexColor("#586B68")
    border = colors.HexColor("#DCE5E0")
    pale = colors.HexColor("#F3F6F3")
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="BGTitle", parent=styles["Title"], fontName="BrandGuardVera-Bold", fontSize=20, leading=25, textColor=ink, alignment=TA_LEFT, spaceAfter=5))
    styles.add(ParagraphStyle(name="BGSubtitle", parent=styles["Normal"], fontName="BrandGuardVera", fontSize=9, leading=13, textColor=muted, spaceAfter=12))
    styles.add(ParagraphStyle(name="BGSection", parent=styles["Heading2"], fontName="BrandGuardVera-Bold", fontSize=11, leading=15, textColor=teal, spaceBefore=14, spaceAfter=7, keepWithNext=True))
    styles.add(ParagraphStyle(name="BGBody", parent=styles["BodyText"], fontName="BrandGuardVera", fontSize=8.5, leading=12, textColor=ink, spaceAfter=4, splitLongWords=1))
    styles.add(ParagraphStyle(name="BGSmall", parent=styles["BodyText"], fontName="BrandGuardVera", fontSize=7.4, leading=10, textColor=muted, spaceAfter=3, splitLongWords=1))
    styles.add(ParagraphStyle(name="BGMeta", parent=styles["BodyText"], fontName="BrandGuardVera", fontSize=7.6, leading=10, textColor=ink, splitLongWords=1))
    styles.add(ParagraphStyle(name="BGQuote", parent=styles["BodyText"], fontName="BrandGuardVera", fontSize=8, leading=11.5, textColor=ink, leftIndent=8, rightIndent=6, borderColor=border, borderWidth=0.7, borderPadding=7, backColor=pale, spaceBefore=4, spaceAfter=7, splitLongWords=1))
    styles.add(ParagraphStyle(name="BGContent", parent=styles["BodyText"], fontName="BrandGuardVera", fontSize=9, leading=13, textColor=ink, spaceAfter=2, splitLongWords=1))

    def paragraph(value, style="BGBody"):
        escaped = escape(str(value)).replace("\t", "    ").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/> ")
        return Paragraph(escaped or "&#160;", styles[style])

    output = BytesIO()
    page_width, page_height = A4

    def page_chrome(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(border)
        canvas.setLineWidth(0.65)
        canvas.line(18 * mm, page_height - 17 * mm, page_width - 18 * mm, page_height - 17 * mm)
        canvas.setFont("BrandGuardVera-Bold", 8)
        canvas.setFillColor(ink)
        canvas.drawString(18 * mm, page_height - 13 * mm, "BrandGuard")
        canvas.setFont("BrandGuardVera", 7)
        canvas.setFillColor(muted)
        canvas.drawRightString(page_width - 18 * mm, 11 * mm, f"Private report · page {doc.page}")
        canvas.restoreState()

    doc = BaseDocTemplate(output, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=23 * mm, bottomMargin=18 * mm, title="BrandGuard approved content and review report", author="BrandGuard", subject="Persisted approval and evidence-backed review record")
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates(PageTemplate(id="BrandGuard", frames=frame, onPage=page_chrome))
    story = [
        paragraph("APPROVED CONTENT · REVIEW REPORT", "BGSmall"),
        paragraph("Approved content and review report", "BGTitle"),
        paragraph("A record of one human decision, one immutable content version, and the policy snapshot used for review.", "BGSubtitle"),
        paragraph("Document information", "BGSection"),
    ]
    metadata = [
        ("Submission", f"{data.submission.title} · {data.submission.pk}"),
        ("Workspace", data.submission.workspace.name),
        ("Channel", data.submission.get_channel_display()),
        ("Content version", f"v{data.version.version_number} · {data.version.pk}"),
        ("Content SHA-256", data.version.content_hash),
        ("Version created", timezone.localtime(data.version.created_at).isoformat()),
        ("Report generated", timezone.localtime(data.generated_at).isoformat()),
    ]
    rows = [[paragraph(label, "BGSmall"), paragraph(value, "BGMeta")] for label, value in metadata]
    table = Table(rows, colWidths=[36 * mm, doc.width - 36 * mm], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.45, border),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(table)
    story.extend([
        paragraph("Human approval", "BGSection"),
        paragraph(f"Decision: {data.decision.get_action_display()} · Decision ID: {data.decision.pk}"),
        paragraph(f"Reviewer: {payload['reviewer']} · User ID: {data.decision.reviewer_id}"),
        paragraph(f"Approved at: {timezone.localtime(data.decision.decided_at).isoformat()} · Request ID: {data.approval.pk}"),
        paragraph("Review and policy identity", "BGSection"),
        paragraph(f"Review run: {data.review.pk} · Status: {data.review.get_status_display()} · Engine: {data.review.engine_version}"),
        paragraph(f"Policy revision: {data.review.policy_revision} · Snapshot SHA-256: {data.review.policy_snapshot_hash}"),
    ])
    for item in data.policy_versions:
        source = item.source_version
        story.append(paragraph(f"Applied source: {source.source.title} · source {source.source_id} · version {source.version_number} · version ID {source.pk} · SHA-256 {item.content_hash}", "BGSmall"))

    story.append(paragraph("Completed deterministic checks", "BGSection"))
    outcome_summary = " · ".join(f"{label}: {count}" for _, label, count in payload["checks_by_outcome"] if count)
    story.append(paragraph(f"Saved checks: {len(data.checks)}" + (f" · {outcome_summary}" if outcome_summary else "")))
    if data.checks:
        check_rows = [[paragraph("Rule / source", "BGSmall"), paragraph("Outcome and saved summary", "BGSmall")]]
        for check in data.checks:
            check_rows.append([
                paragraph(f"{check.rule.identifier} · {check.source_version.source.title} v{check.source_version.version_number}", "BGMeta"),
                paragraph(f"{check.get_outcome_display()} · {check.summary}", "BGMeta"),
            ])
        check_table = Table(check_rows, colWidths=[58 * mm, doc.width - 58 * mm], repeatRows=1, hAlign="LEFT")
        check_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), pale), ("GRID", (0, 0), (-1, -1), 0.4, border),
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(check_table)
    else:
        story.append(paragraph("No structured policy checks were recorded; no compliance conclusion is implied."))

    story.append(paragraph("Findings and verified evidence", "BGSection"))
    story.append(paragraph("Findings by severity: " + ", ".join(f"{label}: {count}" for label, count in payload["severity_counts"])))
    story.append(paragraph(f"Outstanding findings: {len(data.findings)}. Approval and any override do not dismiss findings."))
    if data.findings:
        for finding in data.findings:
            elements = [
                paragraph(f"{finding.rule.identifier} · {finding.rule.get_category_display()} · {finding.rule.get_severity_display()} severity · {finding.get_finding_type_display()}", "BGSection"),
                paragraph(f"Finding ID: {finding.pk} · Status: {finding.get_resolution_status_display()} · Source: {finding.source_version.source.title} v{finding.source_version.version_number} ({finding.source_version.pk})", "BGSmall"),
            ]
            if finding.matched_text:
                elements.append(paragraph("Matched content: " + finding.matched_text))
            elements.append(paragraph("Explanation: " + finding.explanation))
            if finding.suggested_correction:
                elements.append(paragraph("Suggested wording (not automatically applied): " + finding.suggested_correction, "BGSmall"))
            elements.extend([paragraph("Verified approved source evidence", "BGSmall"), paragraph(finding.evidence_excerpt, "BGQuote")])
            story.extend(elements)
    else:
        story.append(paragraph("No finding records were generated by this review run. This is not a universal factual or compliance guarantee."))

    if data.decision.overridden_finding_ids:
        story.extend([
            paragraph("High-severity override · findings remain open", "BGSection"),
            paragraph("The reviewer approved this exact version with a documented override. The underlying finding records remain open."),
            paragraph("Reason: " + data.decision.override_reason),
            paragraph(f"Decision maker: {payload['reviewer']} · Decision ID: {data.decision.pk}"),
        ])
        for finding in payload["overridden_findings"]:
            story.extend([
                paragraph(f"Original finding {finding.rule.identifier} · {finding.pk}", "BGSmall"),
                paragraph("Original matched content: " + (finding.matched_text or "No exact location was recorded."), "BGSmall"),
                paragraph("Original evidence: " + finding.evidence_excerpt, "BGQuote"),
            ])
    elif data.decision.override_reason:
        story.extend([paragraph("Reviewer note", "BGSection"), paragraph(data.decision.override_reason)])

    story.extend([
        paragraph("Limitations", "BGSection"),
        paragraph("This report records BrandGuard's completed deterministic checks and the human approval of this exact version against the recorded snapshot. It is not legal clearance, universal factual verification, accessibility certification, or tamper-proof proof of approval. No AI analysis was performed."),
    ])
    if data.review.not_implemented_checks:
        story.append(paragraph("Checks not implemented: " + "; ".join(str(item) for item in data.review.not_implemented_checks), "BGSmall"))
    if data.review.incomplete_checks:
        story.append(paragraph("Checks recorded as incomplete: " + "; ".join(str(item) for item in data.review.incomplete_checks), "BGSmall"))

    story.extend([paragraph("Approved content · exact saved version", "BGSection")])
    for line in data.version.content_text.splitlines():
        story.append(paragraph(line if line else " ", "BGContent"))
    doc.build(story)
    return output.getvalue()
