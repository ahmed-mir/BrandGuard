import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from approvals.models import ApprovalRequest
from approvals.services import approve_version, current_valid_approval, submit_for_approval
from content_reviews.collaboration import add_review_comment, change_finding_resolution
from content_reviews.models import (
    ContentSubmission,
    ContentVersion,
    FindingResolutionEvent,
    ReviewComment,
    ReviewFinding,
)
from content_reviews.services import create_revision, execute_review
from workspaces.models import Workspace, WorkspaceMembership

from .tests import ReviewFixtureMixin


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ReviewCollaborationTests(ReviewFixtureMixin, TestCase):
    def reviewed(self, text="Meet Luma Desk today."):
        submission, version, run = self.run_review(text)
        finding = run.findings.get(rule__identifier="TERM-001")
        return submission, version, run, finding

    def resolution(self, finding, *, actor=None, new_status, expected_status=None, note="", token=None, submission=None, version=None):
        return change_finding_resolution(
            workspace=self.workspace,
            actor=actor or self.reviewer,
            submission_id=submission.pk if submission else finding.review_run.content_version.submission_id,
            version_number=version.version_number if version else finding.review_run.content_version.version_number,
            finding_id=finding.pk,
            expected_status=expected_status or finding.resolution_status,
            new_status=new_status,
            note=note,
            idempotency_key=token or uuid.uuid4(),
        )

    def test_version_level_creator_and_reviewer_comments_store_role_and_timestamp(self):
        submission, version, _, _ = self.reviewed()
        creator_comment, created = add_review_comment(
            workspace=self.workspace, actor=self.creator, submission_id=submission.pk,
            version_number=version.version_number, body="Please check the updated wording.",
            idempotency_key=uuid.uuid4(),
        )
        reviewer_comment, _ = add_review_comment(
            workspace=self.workspace, actor=self.reviewer, submission_id=submission.pk,
            version_number=version.version_number, body="I will review the evidence.",
            idempotency_key=uuid.uuid4(),
        )
        admin_comment, _ = add_review_comment(
            workspace=self.workspace, actor=self.admin, submission_id=submission.pk,
            version_number=version.version_number, body="Admin note in the same workspace.",
            idempotency_key=uuid.uuid4(),
        )
        self.assertTrue(created)
        self.assertIsNone(creator_comment.finding_id)
        self.assertEqual(creator_comment.author_role, WorkspaceMembership.Role.CREATOR)
        self.assertEqual(reviewer_comment.author_role, WorkspaceMembership.Role.REVIEWER)
        self.assertEqual(admin_comment.author_role, WorkspaceMembership.Role.ADMIN)
        self.assertIsNotNone(creator_comment.created_at)
        self.assertEqual(version.review_comments.count(), 3)

    def test_finding_comment_is_bound_to_exact_finding_and_version(self):
        submission, version, _, finding = self.reviewed()
        comment, created = add_review_comment(
            workspace=self.workspace, actor=self.manager, submission_id=submission.pk,
            version_number=version.version_number, finding_id=finding.pk,
            body="The approved terminology excerpt is clear.", idempotency_key=uuid.uuid4(),
        )
        self.assertTrue(created)
        self.assertEqual(comment.finding_id, finding.pk)
        self.assertEqual(comment.content_version_id, version.pk)
        self.assertEqual(comment.workspace_id, self.workspace.pk)
        self.assertEqual(comment.author_role, WorkspaceMembership.Role.BRAND_MANAGER)

    def test_comments_reject_blank_and_oversized_text(self):
        submission, version, _, _ = self.reviewed()
        for body in ("", "  \n\t  ", "x" * 3001):
            with self.subTest(length=len(body)):
                with self.assertRaises(ValidationError):
                    add_review_comment(
                        workspace=self.workspace, actor=self.creator, submission_id=submission.pk,
                        version_number=version.version_number, body=body,
                        idempotency_key=uuid.uuid4(),
                    )
        self.assertEqual(ReviewComment.objects.count(), 0)

    def test_comment_idempotency_returns_existing_record_and_rejects_token_reuse(self):
        submission, version, _, _ = self.reviewed()
        token = uuid.uuid4()
        kwargs = dict(
            workspace=self.workspace, actor=self.creator, submission_id=submission.pk,
            version_number=version.version_number, body="A single saved comment.", idempotency_key=token,
        )
        first, created = add_review_comment(**kwargs)
        repeated, repeated_created = add_review_comment(**kwargs)
        self.assertTrue(created)
        self.assertFalse(repeated_created)
        self.assertEqual(first.pk, repeated.pk)
        self.assertEqual(ReviewComment.objects.count(), 1)
        with self.assertRaises(ValidationError):
            add_review_comment(**{**kwargs, "body": "Different content with same token."})

    def test_comment_records_cannot_be_edited_or_deleted(self):
        submission, version, _, _ = self.reviewed()
        comment, _ = add_review_comment(
            workspace=self.workspace, actor=self.creator, submission_id=submission.pk,
            version_number=version.version_number, body="Keep this as history.", idempotency_key=uuid.uuid4(),
        )
        comment.body = "Edited after posting"
        with self.assertRaises(ValidationError):
            comment.save()
        with self.assertRaises(ValidationError):
            comment.delete()

    def test_comment_workspace_and_owner_isolation(self):
        submission, version, _, finding = self.reviewed()
        other_workspace = Workspace.objects.create(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
        WorkspaceMembership.objects.create(user=self.manager, workspace=other_workspace, role=WorkspaceMembership.Role.BRAND_MANAGER)
        with self.assertRaises(ContentSubmission.DoesNotExist):
            add_review_comment(
                workspace=other_workspace, actor=self.manager, submission_id=submission.pk,
                version_number=version.version_number, body="Cross workspace.", idempotency_key=uuid.uuid4(),
            )
        with self.assertRaises(ContentSubmission.DoesNotExist):
            add_review_comment(
                workspace=self.workspace, actor=self.other_creator, submission_id=submission.pk,
                version_number=version.version_number, finding_id=finding.pk,
                body="Another creator's private draft.", idempotency_key=uuid.uuid4(),
            )

    def test_default_open_and_valid_resolution_transitions_are_audited(self):
        submission, version, run, finding = self.reviewed()
        original = (finding.classification, finding.evidence_excerpt, finding.review_run.content_version_id)
        self.assertEqual(finding.resolution_status, ReviewFinding.ResolutionStatus.OPEN)
        ack, created = self.resolution(finding, new_status=ReviewFinding.ResolutionStatus.ACKNOWLEDGED)
        self.assertTrue(created)
        resolved, resolved_created = self.resolution(
            finding, new_status=ReviewFinding.ResolutionStatus.RESOLVED,
            expected_status=ReviewFinding.ResolutionStatus.ACKNOWLEDGED,
            note="Updated the wording in version 2.",
        )
        self.assertTrue(resolved_created)
        self.assertEqual((ack.previous_status, ack.new_status), ("OPEN", "ACKNOWLEDGED"))
        self.assertEqual(resolved.actor_id, self.reviewer.pk)
        self.assertEqual(resolved.actor_role, WorkspaceMembership.Role.REVIEWER)
        self.assertIsNotNone(resolved.created_at)
        finding.refresh_from_db()
        self.assertEqual(finding.resolution_status, ReviewFinding.ResolutionStatus.RESOLVED)
        self.assertEqual((finding.classification, finding.evidence_excerpt, finding.review_run.content_version_id), original)
        finding.explanation = "Attempt to rewrite the original deterministic result."
        with self.assertRaises(ValidationError):
            finding.save()
        self.assertEqual(finding.resolution_events.count(), 2)
        self.assertEqual(version.pk, resolved.content_version_id)

    def test_invalid_and_stale_resolution_transitions_are_rejected(self):
        _, _, _, finding = self.reviewed()
        event, _ = self.resolution(finding, new_status=ReviewFinding.ResolutionStatus.ACKNOWLEDGED)
        with self.assertRaises(ValidationError):
            self.resolution(
                finding, new_status=ReviewFinding.ResolutionStatus.WONT_FIX,
                expected_status=ReviewFinding.ResolutionStatus.OPEN,
                note="No longer current.",
            )
        with self.assertRaises(ValidationError):
            self.resolution(
                finding, new_status=ReviewFinding.ResolutionStatus.RESOLVED,
                expected_status=ReviewFinding.ResolutionStatus.OPEN,
                note="Addressed in a draft.",
            )
        self.assertEqual(finding.resolution_events.count(), 1)
        self.assertEqual(event.new_status, ReviewFinding.ResolutionStatus.ACKNOWLEDGED)

    def test_wont_fix_requires_reason_and_does_not_mean_approval(self):
        submission, version, run, finding = self.reviewed()
        with self.assertRaises(ValidationError):
            self.resolution(finding, new_status=ReviewFinding.ResolutionStatus.WONT_FIX, note="No.")
        event, created = self.resolution(
            finding, new_status=ReviewFinding.ResolutionStatus.WONT_FIX,
            note="The team accepts this specific issue without changing it.",
        )
        self.assertTrue(created)
        self.assertEqual(event.new_status, ReviewFinding.ResolutionStatus.WONT_FIX)
        self.assertFalse(ApprovalRequest.objects.filter(submission=submission).exists())
        finding.refresh_from_db()
        self.assertEqual(finding.resolution_status, ReviewFinding.ResolutionStatus.WONT_FIX)

    def test_creator_can_acknowledge_and_can_resolve_only_after_new_version(self):
        submission, version, _, finding = self.reviewed()
        event, _ = self.resolution(
            finding, actor=self.creator, new_status=ReviewFinding.ResolutionStatus.ACKNOWLEDGED
        )
        self.assertEqual(event.actor_role, WorkspaceMembership.Role.CREATOR)
        with self.assertRaises(ValidationError):
            self.resolution(
                finding, actor=self.creator, new_status=ReviewFinding.ResolutionStatus.RESOLVED,
                expected_status=ReviewFinding.ResolutionStatus.ACKNOWLEDGED,
                note="Fixed wording.",
            )
        submission, revised, _ = create_revision(
            submission_id=submission.pk, workspace=self.workspace, actor=self.creator,
            base_version_id=version.pk, base_content_hash=version.content_hash,
            content_text="Meet LumaDesk today.", idempotency_key=uuid.uuid4(),
        )
        resolved, _ = self.resolution(
            finding, actor=self.creator, new_status=ReviewFinding.ResolutionStatus.RESOLVED,
            expected_status=ReviewFinding.ResolutionStatus.ACKNOWLEDGED,
            note="Corrected in the latest content version.",
        )
        self.assertEqual(resolved.content_version_id, version.pk)
        submission.refresh_from_db()
        self.assertEqual(submission.current_version_id, revised.pk)

    def test_creator_cannot_mark_wont_fix_but_reviewer_can(self):
        _, _, _, finding = self.reviewed()
        with self.assertRaises(PermissionDenied):
            self.resolution(
                finding, actor=self.creator, new_status=ReviewFinding.ResolutionStatus.WONT_FIX,
                note="The creator cannot waive this policy issue.",
            )
        self.assertEqual(finding.resolution_events.count(), 0)

    def test_resolution_retries_and_duplicate_effective_state_create_no_extra_event(self):
        _, _, _, finding = self.reviewed()
        token = uuid.uuid4()
        kwargs = dict(
            finding=finding, new_status=ReviewFinding.ResolutionStatus.ACKNOWLEDGED,
            token=token,
        )
        first, created = self.resolution(**kwargs)
        second, repeated_created = self.resolution(**kwargs)
        self.assertTrue(created)
        self.assertFalse(repeated_created)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(finding.resolution_events.count(), 1)
        no_op, no_op_created = self.resolution(
            finding, new_status=ReviewFinding.ResolutionStatus.ACKNOWLEDGED,
            expected_status=ReviewFinding.ResolutionStatus.ACKNOWLEDGED,
        )
        self.assertIsNone(no_op)
        self.assertFalse(no_op_created)
        self.assertEqual(finding.resolution_events.count(), 1)

    def test_resolution_events_are_append_only(self):
        _, _, _, finding = self.reviewed()
        event, _ = self.resolution(finding, new_status=ReviewFinding.ResolutionStatus.ACKNOWLEDGED)
        event.note = "Rewrite history"
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()

    def test_old_version_resolution_history_is_preserved_and_not_copied(self):
        submission, first, _, finding = self.reviewed()
        event, _ = self.resolution(
            finding, new_status=ReviewFinding.ResolutionStatus.ACKNOWLEDGED
        )
        submission, second, _ = create_revision(
            submission_id=submission.pk, workspace=self.workspace, actor=self.creator,
            base_version_id=first.pk, base_content_hash=first.content_hash,
            content_text="The approved product name is LumaDesk.", idempotency_key=uuid.uuid4(),
        )
        second_run = execute_review(
            submission_id=submission.pk,
            version_number=second.version_number,
            workspace=self.workspace,
            actor=self.creator,
            idempotency_key=uuid.uuid4(),
        )
        self.assertEqual(event.content_version_id, first.pk)
        self.assertEqual(finding.resolution_events.count(), 1)
        self.assertEqual(second_run.findings.count(), 0)
        self.assertEqual(second.version_number, 2)
        submission.refresh_from_db()
        self.assertEqual(second.pk, submission.current_version_id)

    def test_resolution_does_not_bypass_high_severity_approval_override_or_stale_export(self):
        submission, version, run, finding = self.reviewed()
        approval, _ = submit_for_approval(
            submission_id=submission.pk, workspace=self.workspace, actor=self.creator, review_run_id=run.pk
        )
        self.resolution(
            finding, new_status=ReviewFinding.ResolutionStatus.WONT_FIX,
            note="The team accepts this issue for this release.",
        )
        with self.assertRaises(ValidationError):
            approve_version(request_id=approval.pk, workspace=self.workspace, actor=self.reviewer)
        decision = approve_version(
            request_id=approval.pk, workspace=self.workspace, actor=self.reviewer,
            override_reason="Reviewed the finding; accepting the documented risk for this release.",
        )
        self.assertEqual(decision.overridden_finding_ids, [str(finding.pk)])
        self.assertTrue(current_valid_approval(submission)["valid"])
        comment, _ = add_review_comment(
            workspace=self.workspace,
            actor=self.creator,
            submission_id=submission.pk,
            version_number=version.version_number,
            finding_id=finding.pk,
            body="The approved wording is retained for this version.",
            idempotency_key=uuid.uuid4(),
        )
        self.assertEqual(comment.finding_id, finding.pk)
        after_comment = current_valid_approval(submission)
        self.assertTrue(after_comment["valid"])
        self.assertEqual(after_comment["decision"].pk, decision.pk)
        # A later operational status change does not rewrite the original review
        # or invalidate an otherwise current approval/export authorization.
        self.resolution(
            finding, new_status=ReviewFinding.ResolutionStatus.OPEN,
            expected_status=ReviewFinding.ResolutionStatus.WONT_FIX,
        )
        self.assertTrue(current_valid_approval(submission)["valid"])

    def test_comment_and_resolution_views_are_post_only_workspace_scoped_and_render_history(self):
        submission, version, run, finding = self.reviewed()
        creator_client = Client()
        creator_client.force_login(self.creator)
        reviewer_client = Client()
        reviewer_client.force_login(self.reviewer)
        version_url = reverse("app:content_reviews:version_detail", args=[submission.pk, version.version_number])
        page = creator_client.get(version_url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Review discussion")
        self.assertEqual(
            creator_client.get(reverse("app:content_reviews:version_comment", args=[submission.pk, version.version_number])).status_code,
            405,
        )
        self.assertEqual(
            reviewer_client.get(reverse("app:content_reviews:finding_resolution", args=[submission.pk, version.version_number, finding.pk])).status_code,
            405,
        )
        response = creator_client.post(
            reverse("app:content_reviews:version_comment", args=[submission.pk, version.version_number]),
            {"body": "Please check this source-backed finding.", "idempotency_key": str(uuid.uuid4())},
        )
        self.assertEqual(response.status_code, 302)
        self.assertContains(creator_client.get(version_url), "Please check this source-backed finding.")
        review_url = reverse("app:content_reviews:review_detail", args=[submission.pk, version.version_number, run.pk])
        self.assertContains(creator_client.get(review_url), "Open")
        finding_url = reverse(
            "app:content_reviews:finding_comment", args=[submission.pk, version.version_number, finding.pk]
        )
        comment_response = reviewer_client.post(
            finding_url,
            {"body": "The citation supports this terminology finding.", "idempotency_key": str(uuid.uuid4())},
        )
        self.assertEqual(comment_response.status_code, 302)
        self.assertContains(reviewer_client.get(review_url), "The citation supports this terminology finding.")
        self.assertEqual(ReviewComment.objects.count(), 2)
        cross_workspace = Workspace.objects.create(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
        outsider = self.make_user("outsider")
        WorkspaceMembership.objects.create(user=outsider, workspace=cross_workspace, role=WorkspaceMembership.Role.REVIEWER)
        outsider_client = Client()
        outsider_client.force_login(outsider)
        self.assertEqual(outsider_client.post(finding_url, {"body": "No access.", "idempotency_key": str(uuid.uuid4())}).status_code, 404)

    def test_duplicate_comment_post_reuses_the_same_token(self):
        submission, version, _, _ = self.reviewed()
        client = Client()
        client.force_login(self.creator)
        url = reverse("app:content_reviews:version_comment", args=[submission.pk, version.version_number])
        token = uuid.uuid4()
        post_data = {"body": "Do not duplicate this note.", "idempotency_key": str(token)}
        first = client.post(url, post_data)
        second = client.post(url, post_data)
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(ReviewComment.objects.filter(idempotency_key=token).count(), 1)

    def test_comment_and_resolution_posts_require_csrf(self):
        submission, version, _, finding = self.reviewed()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.reviewer)
        comment_url = reverse("app:content_reviews:version_comment", args=[submission.pk, version.version_number])
        resolution_url = reverse(
            "app:content_reviews:finding_resolution", args=[submission.pk, version.version_number, finding.pk]
        )
        self.assertEqual(
            client.post(comment_url, {"body": "No CSRF token.", "idempotency_key": str(uuid.uuid4())}).status_code,
            403,
        )
        self.assertEqual(
            client.post(resolution_url, {
                "expected_status": "OPEN", "new_status": "ACKNOWLEDGED",
                "idempotency_key": str(uuid.uuid4()),
            }).status_code,
            403,
        )
        self.assertEqual(ReviewComment.objects.count(), 0)
        self.assertEqual(finding.resolution_events.count(), 0)

    def test_invalid_resolution_state_and_missing_membership_are_denied(self):
        _, _, _, finding = self.reviewed()
        with self.assertRaises(ValidationError):
            self.resolution(finding, new_status="NOT_A_STATE")
        outsider = self.make_user("unassigned")
        with self.assertRaises(PermissionDenied):
            self.resolution(finding, actor=outsider, new_status=ReviewFinding.ResolutionStatus.ACKNOWLEDGED)

    def test_anonymous_comment_post_requires_authentication(self):
        submission, version, _, _ = self.reviewed()
        response = Client().post(
            reverse("app:content_reviews:version_comment", args=[submission.pk, version.version_number]),
            {"body": "A note", "idempotency_key": str(uuid.uuid4())},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_invalid_finding_id_is_not_found(self):
        submission, version, _, _ = self.reviewed()
        client = Client()
        client.force_login(self.reviewer)
        response = client.post(
            reverse("app:content_reviews:finding_resolution", args=[submission.pk, version.version_number, uuid.uuid4()]),
            {
                "expected_status": "OPEN", "new_status": "ACKNOWLEDGED",
                "note": "", "idempotency_key": str(uuid.uuid4()),
            },
        )
        self.assertEqual(response.status_code, 404)
