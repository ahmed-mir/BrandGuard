import uuid

from pypdf import PdfReader

from django.core.exceptions import PermissionDenied
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from approvals.models import ApprovalDecision
from approvals.services import approve_version, current_valid_approval, request_changes, submit_for_approval
from brand_sources.models import BrandSource
from brand_sources.services import approve_source_version, create_source
from workspaces.models import Workspace, WorkspaceMembership

from .exports import (
    approved_export_for,
    build_pdf_report,
    build_text_report,
    safe_export_basename,
)
from .models import ContentSubmission
from .services import create_revision
from .tests import ReviewFixtureMixin


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ApprovedExportTests(ReviewFixtureMixin, TestCase):
    def create_approved(self, text="LumaDesk keeps work clear.", *, approver=None):
        submission, version, review = self.run_review(text)
        request, created = submit_for_approval(
            submission_id=submission.pk,
            workspace=self.workspace,
            actor=self.creator,
            review_run_id=review.pk,
        )
        self.assertTrue(created)
        decision = approve_version(
            request_id=request.pk,
            workspace=self.workspace,
            actor=approver or self.reviewer,
            override_reason=(
                "Reviewed the flagged naming issue and approved this version for the stated campaign."
                if review.findings.filter(rule__severity="HIGH", resolution_status="OPEN").exists()
                else ""
            ),
        )
        return submission, version, review, request, decision

    def url(self, submission, format_name):
        return reverse(f"app:content_reviews:export_{format_name}", args=[submission.pk])

    def test_txt_export_is_exact_utf8_and_has_private_download_headers(self):
        content = "LumaDesk — café launch\nResults vary.\n100% ready."
        submission, version, review, request, decision = self.create_approved(content)
        self.client.force_login(self.creator)
        response = self.client.get(self.url(submission, "txt"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, content.encode("utf-8"))
        self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_pdf_is_valid_and_contains_real_approval_policy_and_evidence(self):
        submission, version, review, request, decision = self.create_approved("Luma Desk brings focus.")
        self.client.force_login(self.creator)
        response = self.client.get(self.url(submission, "pdf"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertTrue(response.content.startswith(b"%PDF-"))
        text = "\n".join(page.extract_text() or "" for page in PdfReader(__import__("io").BytesIO(response.content)).pages)
        for identifier in (str(submission.pk), str(version.pk), str(review.pk), str(decision.pk), review.policy_snapshot_hash):
            self.assertIn(identifier, text)
        self.assertIn("Luma Desk", text)
        self.assertIn("Do not write Luma Desk as two words.", text)
        self.assertIn("Approved content and review report", text)
        self.assertIn("Report generated", text)
        self.assertIn("No AI analysis was performed", text)

    def test_pdf_includes_completed_check_records_and_finding_severity_counts(self):
        submission, version, review, request, decision = self.create_approved("Luma Desk gives teams a start.")
        data = approved_export_for(submission_id=submission.pk, user=self.creator, workspace_id=self.workspace.pk)
        payload = build_text_report(data).decode("utf-8")
        self.assertIn("COMPLETED DETERMINISTIC CHECKS", payload)
        self.assertIn("TERM-001", payload)
        self.assertIn("Findings by severity:", payload)
        self.assertIn("FINDINGS AND VERIFIED EVIDENCE", payload)

    def test_high_severity_override_is_disclosed_with_original_finding_and_reason(self):
        submission, version, review, request, decision = self.create_approved("Luma Desk gives teams a start.")
        self.assertTrue(decision.overridden_finding_ids)
        self.client.force_login(self.creator)
        response = self.client.get(self.url(submission, "pdf"))
        self.assertEqual(response.status_code, 200)
        text = "\n".join(page.extract_text() or "" for page in PdfReader(__import__("io").BytesIO(response.content)).pages)
        self.assertIn("High-severity override", text)
        self.assertIn(decision.override_reason, text)
        original = review.findings.get(pk=decision.overridden_finding_ids[0])
        self.assertIn(original.rule.identifier, text)
        self.assertIn(str(original.pk), text)
        self.assertIn("Do not write Luma Desk as two words.", text)

    def test_pdf_renders_untrusted_markup_as_plain_text(self):
        submission, version, review, request, decision = self.create_approved("LumaDesk uses <script>launch()</script> & stays clear.")
        data = approved_export_for(submission_id=submission.pk, user=self.creator, workspace_id=self.workspace.pk)
        pdf = build_pdf_report(data)
        text = "\n".join(page.extract_text() or "" for page in PdfReader(__import__("io").BytesIO(pdf)).pages)
        self.assertIn("<script>launch()</script> & stays clear.", text)

    def test_export_rechecks_high_severity_override_finding_identity(self):
        submission, version, review, request, decision = self.create_approved("Luma Desk is a deliberately reviewed exception.")
        self.assertTrue(decision.overridden_finding_ids)
        ApprovalDecision.objects.filter(pk=decision.pk).update(overridden_finding_ids=[])
        submission.refresh_from_db()
        self.assertFalse(current_valid_approval(submission)["valid"])
        self.client.force_login(self.creator)
        response = self.client.get(self.url(submission, "pdf"))
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "override", status_code=409)

    def test_pdf_preserves_multiline_content_and_paginates_long_content(self):
        text = "LumaDesk line one.\nResults vary.\n" + ("A concise product sentence with exact punctuation.\n" * 340)
        submission, version, review, request, decision = self.create_approved(text)
        data = approved_export_for(submission_id=submission.pk, user=self.creator, workspace_id=self.workspace.pk)
        content = build_pdf_report(data)
        pages = PdfReader(__import__("io").BytesIO(content)).pages
        self.assertGreater(len(pages), 1)
        rendered = "\n".join(page.extract_text() or "" for page in pages)
        self.assertIn("LumaDesk line one.", rendered)
        self.assertIn("Results vary.", rendered)
        self.assertIn("A concise product sentence with exact punctuation.", rendered)

    def test_filename_is_ascii_safe_and_header_injection_is_impossible(self):
        self.assertEqual(safe_export_basename("\r\nX-Injected: yes / 新 product", 12), "brandguard-x-injected-yes-product-v12")
        submission, version, review, request, decision = self.create_approved()
        ContentSubmission.objects.filter(pk=submission.pk).update(title='Launch\r\nX-Injected: yes')
        self.client.force_login(self.creator)
        response = self.client.get(self.url(submission, "txt"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("\r", response["Content-Disposition"])
        self.assertNotIn("\n", response["Content-Disposition"])
        self.assertNotIn("X-Injected", response.headers)

    def test_anonymous_export_redirects_to_login(self):
        submission, *_ = self.create_approved()
        response = self.client.get(self.url(submission, "txt"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_creator_cannot_export_another_creators_submission(self):
        submission, *_ = self.create_approved()
        self.client.force_login(self.other_creator)
        self.assertEqual(self.client.get(self.url(submission, "txt")).status_code, 404)

    def test_reviewer_manager_and_admin_can_export_current_workspace_approval(self):
        submission, *_ = self.create_approved()
        for user in [self.reviewer, self.manager, self.admin]:
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url(submission, "txt")).status_code, 200)

    def test_cross_workspace_known_id_is_not_found(self):
        submission, *_ = self.create_approved()
        other_workspace = Workspace.objects.create(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
        outsider = self.make_user("exportoutsider")
        WorkspaceMembership.objects.create(user=outsider, workspace=other_workspace, role=WorkspaceMembership.Role.REVIEWER)
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(self.url(submission, "pdf")).status_code, 404)

    def test_service_rejects_anonymous_user_and_wrong_workspace(self):
        submission, *_ = self.create_approved()
        with self.assertRaises(PermissionDenied):
            approved_export_for(submission_id=submission.pk, user=type("Anonymous", (), {"is_authenticated": False, "is_active": False})())
        with self.assertRaises(PermissionDenied):
            approved_export_for(submission_id=submission.pk, user=self.creator, workspace_id=uuid.uuid4())

    def test_unapproved_and_pending_submissions_cannot_export(self):
        submission, version, review = self.run_review("LumaDesk has clear guidance.")
        self.client.force_login(self.creator)
        response = self.client.get(self.url(submission, "txt"))
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "No approved version", status_code=409)
        approval, _ = submit_for_approval(submission_id=submission.pk, workspace=self.workspace, actor=self.creator, review_run_id=review.pk)
        response = self.client.get(self.url(submission, "pdf"))
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "No approved version", status_code=409)

    def test_changes_requested_request_cannot_export(self):
        submission, version, review = self.run_review("LumaDesk copy awaits a human decision.")
        approval, _ = submit_for_approval(
            submission_id=submission.pk, workspace=self.workspace,
            actor=self.creator, review_run_id=review.pk,
        )
        request_changes(
            request_id=approval.pk, workspace=self.workspace, actor=self.reviewer,
            comment="Please make the product naming clearer for this audience.",
        )
        self.client.force_login(self.creator)
        response = self.client.get(self.url(submission, "txt"))
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "No approved version", status_code=409)

    def test_pdf_refuses_unrepresentable_unicode_instead_of_dropping_it(self):
        submission, *_ = self.create_approved("LumaDesk launch 🚀")
        self.client.force_login(self.creator)
        response = self.client.get(self.url(submission, "pdf"))
        self.assertEqual(response.status_code, 422)
        self.assertContains(response, "cannot represent", status_code=422)
        self.assertEqual(self.client.get(self.url(submission, "txt")).status_code, 200)

    def test_new_content_invalidates_both_export_routes(self):
        submission, version, review, request, decision = self.create_approved()
        create_revision(
            submission_id=submission.pk, workspace=self.workspace, actor=self.creator,
            base_version_id=version.pk, base_content_hash=version.content_hash,
            content_text="LumaDesk has revised copy.", idempotency_key=uuid.uuid4(),
        )
        submission.refresh_from_db()
        self.assertFalse(current_valid_approval(submission)["valid"])
        self.client.force_login(self.creator)
        for ext in ["txt", "pdf"]:
            response = self.client.get(self.url(submission, ext))
            self.assertEqual(response.status_code, 409)
            self.assertContains(response, "content changed", status_code=409)

    def test_new_policy_snapshot_invalidates_both_export_routes(self):
        submission, version, review, request, decision = self.create_approved()
        source, draft = create_source(
            workspace=self.workspace, owner=self.manager, title="Updated export policy",
            source_type=BrandSource.SourceType.POLICY, description="A real policy update",
            source_text="A newly active workspace policy for export invalidation tests.",
        )
        approve_source_version(source_id=source.pk, version_number=draft.version_number, workspace=self.workspace, actor=self.manager)
        self.client.force_login(self.creator)
        stale_decision = self.client.get(reverse("app:approvals:detail", args=[request.pk]))
        self.assertContains(stale_decision, "Open current version and review")
        for ext in ["txt", "pdf"]:
            response = self.client.get(self.url(submission, ext))
            self.assertEqual(response.status_code, 409)
            self.assertContains(response, "policy changed", status_code=409)

    def test_old_approved_version_cannot_bypass_current_version_export_guard(self):
        submission, version, review, request, decision = self.create_approved()
        create_revision(
            submission_id=submission.pk, workspace=self.workspace, actor=self.creator,
            base_version_id=version.pk, base_content_hash=version.content_hash,
            content_text="Current unapproved version.", idempotency_key=uuid.uuid4(),
        )
        # Export endpoint only accepts a submission ID and always resolves its current version.
        self.client.force_login(self.creator)
        response = self.client.get(self.url(submission, "txt"))
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("LumaDesk keeps work clear.", response.content.decode())

    def test_pdf_and_txt_ui_links_appear_only_for_current_valid_approval(self):
        submission, version, review, request, decision = self.create_approved()
        self.client.force_login(self.creator)
        page = self.client.get(reverse("app:content_reviews:version_detail", args=[submission.pk, version.version_number]))
        self.assertContains(page, reverse("app:content_reviews:export_txt", args=[submission.pk]))
        self.assertContains(page, reverse("app:content_reviews:export_pdf", args=[submission.pk]))
        self.assertContains(page, "Currently approved")
        approval_page = self.client.get(reverse("app:approvals:detail", args=[request.pk]))
        self.assertContains(approval_page, reverse("app:content_reviews:export_txt", args=[submission.pk]))
        self.assertContains(approval_page, reverse("app:content_reviews:export_pdf", args=[submission.pk]))
        create_revision(
            submission_id=submission.pk, workspace=self.workspace, actor=self.creator,
            base_version_id=version.pk, base_content_hash=version.content_hash,
            content_text="New version not approved.", idempotency_key=uuid.uuid4(),
        )
        stale_page = self.client.get(reverse("app:content_reviews:version_detail", args=[submission.pk, version.version_number]))
        self.assertNotContains(stale_page, reverse("app:content_reviews:export_txt", args=[submission.pk]))
        self.assertContains(stale_page, "Export is blocked until the current version receives a fresh complete review and human approval.")

    def test_end_to_end_real_engine_review_approval_download_and_policy_invalidation(self):
        creator_client = Client()
        creator_client.force_login(self.creator)
        posted = creator_client.post(reverse("app:content_reviews:create"), {
            "idempotency_key": str(uuid.uuid4()),
            "title": "Approved launch copy", "channel": ContentSubmission.Channel.INSTAGRAM,
            "audience": "Operations teams", "campaign_purpose": "Product launch",
            "content_text": "Luma Desk makes task completion clearer.",
        })
        self.assertEqual(posted.status_code, 302)
        submission = ContentSubmission.objects.get(title="Approved launch copy")
        version = submission.current_version
        from .services import execute_review
        run = execute_review(
            submission_id=submission.pk, version_number=version.version_number,
            workspace=self.workspace, actor=self.creator, idempotency_key=uuid.uuid4(),
        )
        self.assertTrue(run.findings.filter(rule__identifier="TERM-001").exists())
        approval, _ = submit_for_approval(
            submission_id=submission.pk, workspace=self.workspace, actor=self.creator, review_run_id=run.pk,
        )
        self.client.force_login(self.reviewer)
        detail = self.client.get(reverse("app:approvals:detail", args=[approval.pk]))
        self.assertContains(detail, "Do not write Luma Desk as two words.")
        decision = approve_version(request_id=approval.pk, workspace=self.workspace, actor=self.reviewer, override_reason="Reviewed the naming finding and accepted this exact version.")
        self.assertEqual(decision.action, ApprovalDecision.Action.APPROVED)
        self.client.force_login(self.creator)
        txt = self.client.get(self.url(submission, "txt"))
        pdf = self.client.get(self.url(submission, "pdf"))
        self.assertEqual(txt.status_code, 200)
        self.assertEqual(txt.content, version.content_text.encode("utf-8"))
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(PdfReader(__import__("io").BytesIO(pdf.content)).pages)
        source, draft = create_source(
            workspace=self.workspace, owner=self.manager, title="New policy after export",
            source_type=BrandSource.SourceType.POLICY, description="Changes current policy",
            source_text="An actual approved source version that invalidates a previous approval.",
        )
        approve_source_version(source_id=source.pk, version_number=draft.version_number, workspace=self.workspace, actor=self.manager)
        for ext in ["txt", "pdf"]:
            self.assertEqual(self.client.get(self.url(submission, ext)).status_code, 409)

