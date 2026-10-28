import uuid

from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings
from django.urls import reverse

from approvals.services import approve_version, request_changes, submit_for_approval
from brand_sources.models import BrandSource
from brand_sources.services import approve_source_version, create_source
from content_reviews.collaboration import change_finding_resolution
from content_reviews.models import ContentSubmission, ReviewFinding, ReviewRun
from content_reviews.readiness import build_review_readiness
from content_reviews.services import create_revision, execute_review
from workspaces.models import Workspace, WorkspaceMembership

from .tests import ReviewFixtureMixin


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ReviewReadinessTests(ReviewFixtureMixin, TestCase):
    def readiness(self, submission, version, user=None):
        membership = WorkspaceMembership.objects.get(
            workspace=self.workspace, user=user or self.creator, is_active=True
        )
        return build_review_readiness(
            workspace=self.workspace,
            membership=membership,
            submission=submission,
            version=version,
        )

    def create_approval(self, *, text="LumaDesk is clear."):
        submission, version, review = self.run_review(text)
        request, _ = submit_for_approval(
            submission_id=submission.pk,
            workspace=self.workspace,
            actor=self.creator,
            review_run_id=review.pk,
        )
        return submission, version, review, request

    def test_new_current_version_is_ready_for_review_for_its_creator(self):
        submission, version = self.create_submission_for(text="LumaDesk is clear.")
        result = self.readiness(submission, version)
        self.assertEqual(result["stage"], "READY_FOR_REVIEW")
        self.assertEqual(result["next_action"]["label"], "Run policy review")
        self.assertEqual(result["policy"]["label"], "Policy ready")

    def test_submission_without_a_saved_version_points_to_content_creation(self):
        submission = ContentSubmission.objects.create(
            workspace=self.workspace,
            creator=self.creator,
            title="Unversioned draft",
            channel=ContentSubmission.Channel.EMAIL,
        )
        result = self.readiness(submission, None)
        self.assertEqual(result["stage"], "DRAFT")
        self.assertEqual(result["next_action"]["label"], "New content")

    def test_failed_review_is_not_described_as_complete(self):
        submission, version, run = self.run_review("LumaDesk is clear.")
        ReviewRun.objects.filter(pk=run.pk).update(status=ReviewRun.Status.FAILED)
        result = self.readiness(submission, version)
        self.assertEqual(result["stage"], "REVIEW_FAILED")
        self.assertFalse(result["review"]["fresh"])
        self.assertEqual(result["next_action"]["label"], "Run policy review")

    def test_partial_review_is_not_ready_for_approval(self):
        submission, version, run = self.run_review("LumaDesk is clear.")
        ReviewRun.objects.filter(pk=run.pk).update(status=ReviewRun.Status.PARTIAL)
        result = self.readiness(submission, version)
        self.assertEqual(result["stage"], "REVIEW_PARTIAL")
        self.assertFalse(result["review"]["fresh"])
        self.assertEqual(result["next_action"]["label"], "Run policy review")

    def test_missing_policy_directs_manager_to_source_library_and_creator_to_manager(self):
        workspace = Workspace.objects.create(name="Empty policy", slug=f"empty-policy-{uuid.uuid4().hex[:8]}")
        creator = self.make_user("no_policy_creator")
        manager = self.make_user("no_policy_manager")
        WorkspaceMembership.objects.create(user=creator, workspace=workspace, role=WorkspaceMembership.Role.CREATOR)
        WorkspaceMembership.objects.create(user=manager, workspace=workspace, role=WorkspaceMembership.Role.BRAND_MANAGER)
        from content_reviews.services import create_submission
        submission, version = create_submission(
            workspace=workspace,
            creator=creator,
            title="No policy copy",
            channel="EMAIL",
            audience="Teams",
            campaign_purpose="Launch",
            content_text="A new message",
        )
        creator_result = build_review_readiness(
            workspace=workspace,
            membership=WorkspaceMembership.objects.get(workspace=workspace, user=creator),
            submission=submission,
            version=version,
        )
        manager_result = build_review_readiness(
            workspace=workspace,
            membership=WorkspaceMembership.objects.get(workspace=workspace, user=manager),
            submission=submission,
            version=version,
        )
        self.assertEqual(creator_result["stage"], "POLICY_REQUIRED")
        self.assertIn("brand manager", creator_result["next_action"]["description"])
        self.assertEqual(manager_result["next_action"]["label"], "Open Brand Sources")
        self.assertTrue(manager_result["next_action"]["url"].endswith("/app/brand-sources/"))

    def test_complete_fresh_review_with_findings_recommends_reviewing_them(self):
        submission, version, _run = self.run_review("Luma Desk is a clear tool.")
        result = self.readiness(submission, version)
        self.assertEqual(result["stage"], "REVISION_RECOMMENDED")
        self.assertEqual(result["review"]["count"], 1)
        self.assertEqual(result["review"]["resolutions"][ReviewFinding.ResolutionStatus.OPEN], 1)
        self.assertIn("does not equal approval", result["resolution_note"])

    def test_resolved_finding_remains_in_total_and_resolution_summary(self):
        submission, version, run = self.run_review("Luma Desk is a clear tool.")
        finding = run.findings.get()
        version2 = create_revision(
            submission_id=submission.pk,
            workspace=self.workspace,
            actor=self.creator,
            base_version_id=version.pk,
            base_content_hash=version.content_hash,
            content_text="LumaDesk is a clear tool.",
            idempotency_key=uuid.uuid4(),
        )[1]
        event, _ = change_finding_resolution(
            workspace=self.workspace, actor=self.reviewer,
            submission_id=submission.pk, version_number=version.version_number,
            finding_id=finding.pk, expected_status=ReviewFinding.ResolutionStatus.OPEN,
            new_status=ReviewFinding.ResolutionStatus.ACKNOWLEDGED, idempotency_key=uuid.uuid4(),
        )
        change_finding_resolution(
            workspace=self.workspace, actor=self.reviewer,
            submission_id=submission.pk, version_number=version.version_number,
            finding_id=finding.pk, expected_status=event.new_status,
            new_status=ReviewFinding.ResolutionStatus.RESOLVED, note="Updated the product spelling in v2.",
            idempotency_key=uuid.uuid4(),
        )
        historical = self.readiness(submission, version)
        self.assertEqual(historical["stage"], "HISTORICAL")
        self.assertEqual(historical["review"]["count"], 1)
        self.assertEqual(historical["review"]["resolutions"][ReviewFinding.ResolutionStatus.RESOLVED], 1)
        self.assertEqual(version2.version_number, 2)
        self.assertEqual(version2.review_runs.count(), 0)

    def test_completed_clean_review_is_ready_to_request_approval(self):
        submission, version, _run = self.run_review("LumaDesk is a clear tool.")
        result = self.readiness(submission, version)
        self.assertEqual(result["stage"], "READY_FOR_APPROVAL")
        self.assertTrue(result["review"]["fresh"])
        self.assertEqual(result["next_action"]["url"], "#readiness-submit")

    def test_pending_request_has_creator_wait_state_and_reviewer_action(self):
        submission, version, _run, request = self.create_approval()
        creator_result = self.readiness(submission, version, self.creator)
        reviewer_result = self.readiness(submission, version, self.reviewer)
        self.assertEqual(creator_result["stage"], "APPROVAL_PENDING")
        self.assertIn("reviewer", creator_result["next_action"]["description"])
        self.assertEqual(reviewer_result["stage"], "APPROVAL_PENDING")
        self.assertEqual(reviewer_result["next_action"]["url"], reverse("app:approvals:detail", args=[request.pk]))
        self.client.force_login(self.creator)
        response = self.client.get(reverse("app:content_reviews:version_detail", args=[submission.pk, version.version_number]))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["submit_form"])
        self.assertFalse(response.context["review_readiness"]["can_run_review"])

    def test_changes_requested_directs_creator_to_revision(self):
        submission, version, _review, request = self.create_approval()
        request_changes(
            request_id=request.pk, workspace=self.workspace, actor=self.reviewer,
            comment="Please clarify the audience and soften this copy.",
        )
        result = self.readiness(submission, version)
        self.assertEqual(result["stage"], "CHANGES_REQUESTED")
        self.assertEqual(result["next_action"]["label"], "Revise content")

    def test_approved_current_version_is_export_ready(self):
        submission, version, _review, request = self.create_approval()
        approve_version(request_id=request.pk, workspace=self.workspace, actor=self.reviewer)
        result = self.readiness(submission, version)
        self.assertEqual(result["stage"], "READY_TO_EXPORT")
        self.assertTrue(result["export"]["available"])
        self.assertEqual(result["approval"]["label"], "Approved")

    def test_content_change_stales_approval_but_points_new_version_to_review(self):
        submission, version, _review, request = self.create_approval()
        approve_version(request_id=request.pk, workspace=self.workspace, actor=self.reviewer)
        _submission, revised, _created = create_revision(
            submission_id=submission.pk, workspace=self.workspace, actor=self.creator,
            base_version_id=version.pk, base_content_hash=version.content_hash,
            content_text="LumaDesk is clear and ready.", idempotency_key=uuid.uuid4(),
        )
        result = self.readiness(submission, revised)
        self.assertEqual(result["stage"], "APPROVAL_STALE")
        self.assertFalse(result["export"]["available"])
        self.assertEqual(result["next_action"]["label"], "Run policy review")
        fresh_run = execute_review(
            submission_id=submission.pk,
            version_number=revised.version_number,
            workspace=self.workspace,
            actor=self.creator,
            idempotency_key=uuid.uuid4(),
        )
        refreshed = self.readiness(submission, revised)
        self.assertEqual(refreshed["stage"], "APPROVAL_STALE")
        self.assertEqual(refreshed["next_action"]["label"], "Go to approval submission")
        self.client.force_login(self.creator)
        response = self.client.get(reverse("app:content_reviews:version_detail", args=[submission.pk, revised.version_number]))
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context["submit_form"])
        self.assertEqual(response.context["submit_form"].initial["review_run_id"], fresh_run.pk)

    def test_policy_change_stales_approval_and_requires_new_version(self):
        submission, version, _review, request = self.create_approval()
        approve_version(request_id=request.pk, workspace=self.workspace, actor=self.reviewer)
        source, draft = create_source(
            workspace=self.workspace, owner=self.manager, title="Updated policy note",
            source_type=BrandSource.SourceType.GUIDELINES,
            description="Policy change for readiness test.", source_text="Use concise, accurate product copy.",
        )
        approve_source_version(source_id=source.pk, version_number=draft.version_number, workspace=self.workspace, actor=self.manager)
        result = self.readiness(submission, version)
        self.assertEqual(result["stage"], "APPROVAL_STALE")
        self.assertFalse(result["export"]["available"])
        self.assertEqual(result["next_action"]["label"], "Create a new version")

    def test_policy_change_after_review_requires_fresh_deterministic_review(self):
        submission, version, _run = self.run_review("LumaDesk is clear.")
        source, draft = create_source(
            workspace=self.workspace, owner=self.manager, title="New policy note",
            source_type=BrandSource.SourceType.GUIDELINES,
            description="Policy change for stale review test.", source_text="Use concise, accurate product copy.",
        )
        approve_source_version(source_id=source.pk, version_number=draft.version_number, workspace=self.workspace, actor=self.manager)
        result = self.readiness(submission, version)
        self.assertEqual(result["stage"], "REVIEW_STALE")
        self.assertEqual(result["policy"]["label"], "Policy changed since review")
        self.assertEqual(result["next_action"]["label"], "Run policy review")

    def test_high_severity_override_is_still_required_after_finding_resolution(self):
        submission, version, run = self.run_review("Luma Desk is a clear tool.")
        result = self.readiness(submission, version)
        self.assertTrue(result["approval"]["override_required"])
        finding = run.findings.get()
        self.assertEqual(finding.resolution_status, ReviewFinding.ResolutionStatus.OPEN)

        # Operational resolution is not an approval override and cannot clear
        # the approval service's requirement for a documented reviewer reason.
        change_finding_resolution(
            workspace=self.workspace,
            actor=self.reviewer,
            submission_id=submission.pk,
            version_number=version.version_number,
            finding_id=finding.pk,
            expected_status=ReviewFinding.ResolutionStatus.OPEN,
            new_status=ReviewFinding.ResolutionStatus.RESOLVED,
            note="Updated the copy in a new saved version.",
            idempotency_key=uuid.uuid4(),
        )
        resolved_result = self.readiness(submission, version)
        self.assertTrue(resolved_result["approval"]["override_required"])
        self.assertEqual(resolved_result["review"]["count"], 1)
        self.assertEqual(
            resolved_result["review"]["resolutions"][ReviewFinding.ResolutionStatus.RESOLVED], 1
        )

    def test_readiness_view_is_rendered_for_creator_and_reviewer(self):
        submission, version, _run = self.run_review("Luma Desk is a clear tool.")
        url = reverse("app:content_reviews:version_detail", args=[submission.pk, version.version_number])
        for user in (self.creator, self.reviewer, self.manager):
            with self.subTest(role=WorkspaceMembership.objects.get(user=user, workspace=self.workspace).role):
                self.client.force_login(user)
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Review readiness")
                self.assertContains(response, "Finding resolution tracks collaboration state and does not equal approval.")

    def test_readiness_rejects_cross_workspace_submission(self):
        other = Workspace.objects.create(name="Other workspace", slug=f"other-{uuid.uuid4().hex[:8]}")
        submission, version = self.create_submission_for(text="LumaDesk is clear.")
        membership = WorkspaceMembership.objects.create(
            user=self.make_user("other_manager"), workspace=other,
            role=WorkspaceMembership.Role.BRAND_MANAGER,
        )
        with self.assertRaises(PermissionDenied):
            build_review_readiness(workspace=other, membership=membership, submission=submission, version=version)

    def test_readiness_is_read_only_for_reviews_findings_and_approvals(self):
        submission, version, run = self.run_review("Luma Desk is a clear tool.")
        before = (run.pk, run.findings.count(), submission.approval_requests.count())
        self.readiness(submission, version)
        self.assertEqual((run.pk, run.findings.count(), submission.approval_requests.count()), before)
