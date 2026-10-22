import re
import uuid

from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from approvals.models import ApprovalDecision, ApprovalRequest
from approvals.services import (
    approve_version,
    current_valid_approval,
    request_changes,
    submit_for_approval,
)
from brand_sources.models import BrandSource
from brand_sources.services import approve_source_version, create_source
from workspaces.models import WorkspaceMembership

from .models import ContentSubmission, ContentVersion, ReviewRun
from .services import create_revision, execute_review
from .tests import ReviewFixtureMixin


def hidden_value(response, field):
    match = re.search(rf'name="{field}"[^>]*value="([^"]+)"', response.content.decode())
    if not match:
        match = re.search(rf'name="{field}"[^>]*value=&#x27;([^&#]+)&#x27;', response.content.decode())
    if not match:
        raise AssertionError(f"Hidden field {field} not found in response")
    return match.group(1)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class RevisionAndApprovalServiceTests(ReviewFixtureMixin, TestCase):
    def reviewed_submission(self, text="Luma Desk is a useful tool."):
        submission, version, run = self.run_review(text)
        self.assertEqual(run.status, ReviewRun.Status.COMPLETE)
        self.assertTrue(run.checks.exists())
        return submission, version, run

    def create_request(self, *, text="LumaDesk brings focus."):
        submission, version, review = self.reviewed_submission(text)
        approval, created = submit_for_approval(
            submission_id=submission.pk, workspace=self.workspace,
            actor=self.creator, review_run_id=review.pk,
        )
        self.assertTrue(created)
        return submission, version, review, approval

    def test_revision_appends_immutable_version_and_uses_safe_number_and_hash(self):
        submission, first, _ = self.reviewed_submission()
        original = first.content_text
        updated, second, created = create_revision(
            submission_id=submission.pk, workspace=self.workspace, actor=self.creator,
            base_version_id=first.pk, base_content_hash=first.content_hash,
            content_text="LumaDesk is the approved spelling.", idempotency_key=uuid.uuid4(),
        )
        self.assertTrue(created)
        self.assertEqual(second.version_number, 2)
        self.assertEqual(second.revision_of_id, first.pk)
        self.assertEqual(updated.current_version_id, second.pk)
        self.assertNotEqual(second.content_hash, first.content_hash)
        first.refresh_from_db()
        self.assertEqual(first.content_text, original)
        self.assertEqual(first.review_runs.count(), 1)
        self.assertEqual(second.review_runs.count(), 0)

    def test_revision_idempotency_returns_the_same_record(self):
        submission, first, _ = self.reviewed_submission()
        token = uuid.uuid4()
        kwargs = dict(
            submission_id=submission.pk, workspace=self.workspace, actor=self.creator,
            base_version_id=first.pk, base_content_hash=first.content_hash,
            content_text="Updated saved content.", idempotency_key=token,
        )
        _, one, created_one = create_revision(**kwargs)
        _, two, created_two = create_revision(**kwargs)
        self.assertTrue(created_one)
        self.assertFalse(created_two)
        self.assertEqual(one.pk, two.pk)
        self.assertEqual(submission.versions.count(), 2)

    def test_stale_revision_base_is_rejected(self):
        submission, first, _ = self.reviewed_submission()
        _, _, _ = create_revision(
            submission_id=submission.pk, workspace=self.workspace, actor=self.creator,
            base_version_id=first.pk, base_content_hash=first.content_hash,
            content_text="First session's revision.", idempotency_key=uuid.uuid4(),
        )
        with self.assertRaises(ValidationError):
            create_revision(
                submission_id=submission.pk, workspace=self.workspace, actor=self.creator,
                base_version_id=first.pk, base_content_hash=first.content_hash,
                content_text="A stale second session revision.", idempotency_key=uuid.uuid4(),
            )

    def test_creator_cannot_revise_another_owners_submission(self):
        submission, version, _ = self.reviewed_submission()
        with self.assertRaises(ContentSubmission.DoesNotExist):
            create_revision(
                submission_id=submission.pk, workspace=self.workspace, actor=self.other_creator,
                base_version_id=version.pk, base_content_hash=version.content_hash,
                content_text="Attempt to change another creator's content.", idempotency_key=uuid.uuid4(),
            )

    def test_review_only_exact_current_fresh_complete_version_can_be_submitted(self):
        submission, version, review = self.reviewed_submission()
        new_source, draft = create_source(
            workspace=self.workspace, owner=self.manager, title="New current policy",
            source_type=BrandSource.SourceType.POLICY, description="Policy update",
            source_text="An approved policy update for testing.",
        )
        approve_source_version(source_id=new_source.pk, version_number=draft.version_number, workspace=self.workspace, actor=self.manager)
        with self.assertRaisesMessage(ValidationError, "Approved brand policy changed"):
            submit_for_approval(submission_id=submission.pk, workspace=self.workspace, actor=self.creator, review_run_id=review.pk)

    def test_incomplete_review_cannot_be_submitted(self):
        submission, version, review = self.reviewed_submission()
        partial = ReviewRun.objects.create(
            workspace=self.workspace,
            content_version=version,
            requested_by=self.creator,
            status=ReviewRun.Status.PARTIAL,
            started_at=timezone.now(),
            completed_at=timezone.now(),
            policy_revision=review.policy_revision,
            policy_snapshot_hash=review.policy_snapshot_hash,
            policy_snapshot=review.policy_snapshot,
            engine_version=review.engine_version,
            idempotency_key=uuid.uuid4(),
        )
        with self.assertRaises(ValidationError):
            submit_for_approval(submission_id=submission.pk, workspace=self.workspace, actor=self.creator, review_run_id=partial.pk)

    def test_duplicate_active_request_is_idempotent_and_not_duplicated(self):
        submission, version, review, first = self.create_request()
        same, created = submit_for_approval(submission_id=submission.pk, workspace=self.workspace, actor=self.creator, review_run_id=review.pk)
        self.assertFalse(created)
        self.assertEqual(first.pk, same.pk)
        self.assertEqual(ApprovalRequest.objects.filter(content_version=version).count(), 1)

    def test_non_creator_cannot_submit(self):
        submission, version, review = self.reviewed_submission()
        with self.assertRaises(ValidationError):
            submit_for_approval(submission_id=submission.pk, workspace=self.workspace, actor=self.manager, review_run_id=review.pk)

    def test_changes_requested_decision_is_persisted_and_immutable(self):
        submission, version, review, approval = self.create_request()
        decision = request_changes(request_id=approval.pk, workspace=self.workspace, actor=self.reviewer, comment="Please refine the opening copy.")
        approval.refresh_from_db()
        self.assertEqual(approval.status, ApprovalRequest.Status.CHANGES_REQUESTED)
        self.assertEqual(decision.reviewer_id, self.reviewer.pk)
        self.assertEqual(decision.comment, "Please refine the opening copy.")
        with self.assertRaises(ValidationError):
            decision.comment = "overwrite"
            decision.save()

    def test_high_severity_finding_requires_reason_and_override_preserves_open_finding(self):
        submission, version, review, approval = self.create_request(text="Luma Desk is here.")
        finding = review.findings.get(rule__identifier="TERM-001")
        with self.assertRaisesMessage(ValidationError, "unresolved high-severity findings"):
            approve_version(request_id=approval.pk, workspace=self.workspace, actor=self.reviewer)
        with self.assertRaises(ValidationError):
            approve_version(request_id=approval.pk, workspace=self.workspace, actor=self.reviewer, override_reason="ok")
        decision = approve_version(
            request_id=approval.pk, workspace=self.workspace, actor=self.reviewer,
            override_reason="Approved after reviewing the naming exception with the campaign owner.",
        )
        finding.refresh_from_db()
        self.assertEqual(decision.action, ApprovalDecision.Action.APPROVED)
        self.assertEqual(decision.overridden_finding_ids, [str(finding.pk)])
        self.assertEqual(finding.resolution_status, "OPEN")
        self.assertTrue(current_valid_approval(submission)["valid"])

    def test_creator_self_approval_and_brand_manager_approval_are_denied(self):
        submission, version, review, approval = self.create_request()
        # Model a future role expansion for a user who authored the submission;
        # self-approval must remain forbidden independently of the role label.
        WorkspaceMembership.objects.filter(user=self.creator, workspace=self.workspace).update(role=WorkspaceMembership.Role.REVIEWER)
        with self.assertRaises(ValidationError):
            approve_version(request_id=approval.pk, workspace=self.workspace, actor=self.creator)
        with self.assertRaises(ValidationError):
            approve_version(request_id=approval.pk, workspace=self.workspace, actor=self.manager)

    def test_decision_is_bound_to_exact_version_hash_and_policy_snapshot(self):
        submission, version, review, approval = self.create_request()
        decision = approve_version(request_id=approval.pk, workspace=self.workspace, actor=self.reviewer)
        self.assertEqual(decision.content_hash, version.content_hash)
        self.assertEqual(decision.policy_revision, review.policy_revision)
        self.assertEqual(decision.policy_snapshot_hash, review.policy_snapshot_hash)
        self.assertEqual(decision.policy_snapshot, review.policy_snapshot)
        self.assertEqual(decision.policy_snapshot["versions"], review.policy_snapshot["versions"])

    def test_new_content_version_invalidates_current_approval_without_deleting_history(self):
        submission, version, review, approval = self.create_request()
        approve_version(request_id=approval.pk, workspace=self.workspace, actor=self.reviewer)
        _, revised, _ = create_revision(
            submission_id=submission.pk, workspace=self.workspace, actor=self.creator,
            base_version_id=version.pk, base_content_hash=version.content_hash,
            content_text="A new current version.", idempotency_key=uuid.uuid4(),
        )
        submission.refresh_from_db()
        current = current_valid_approval(submission)
        self.assertFalse(current["valid"])
        self.assertIn("content changed", current["reason"])
        self.assertTrue(ApprovalDecision.objects.filter(approval_request=approval).exists())
        self.assertEqual(submission.current_version_id, revised.pk)

    def test_policy_change_invalidates_current_approval_without_erasing_decision(self):
        submission, version, review, approval = self.create_request()
        decision = approve_version(request_id=approval.pk, workspace=self.workspace, actor=self.reviewer)
        _, draft = create_source(
            workspace=self.workspace, owner=self.manager, title="Policy after approval",
            source_type=BrandSource.SourceType.GUIDELINES, description="Changed policy",
            source_text="New approved policy is effective.",
        )
        approve_source_version(source_id=draft.source_id, version_number=draft.version_number, workspace=self.workspace, actor=self.manager)
        current = current_valid_approval(submission)
        self.assertFalse(current["valid"])
        self.assertIn("policy changed", current["reason"])
        self.assertTrue(ApprovalDecision.objects.filter(pk=decision.pk).exists())

    def test_double_decision_is_rejected(self):
        submission, version, review, approval = self.create_request()
        approve_version(request_id=approval.pk, workspace=self.workspace, actor=self.reviewer)
        with self.assertRaises(ValidationError):
            approve_version(request_id=approval.pk, workspace=self.workspace, actor=self.admin)

    def test_admin_can_approve_another_creators_exact_version(self):
        submission, version, review, approval = self.create_request()
        decision = approve_version(request_id=approval.pk, workspace=self.workspace, actor=self.admin)
        self.assertEqual(decision.action, ApprovalDecision.Action.APPROVED)
        self.assertEqual(decision.reviewer_id, self.admin.pk)

    def test_pending_request_cannot_be_decided_after_content_becomes_stale(self):
        submission, version, review, approval = self.create_request()
        create_revision(
            submission_id=submission.pk, workspace=self.workspace, actor=self.creator,
            base_version_id=version.pk, base_content_hash=version.content_hash,
            content_text="A later content version.", idempotency_key=uuid.uuid4(),
        )
        with self.assertRaisesMessage(ValidationError, "newer content version is current"):
            approve_version(request_id=approval.pk, workspace=self.workspace, actor=self.reviewer)
        self.assertFalse(ApprovalDecision.objects.filter(approval_request=approval).exists())

    def test_stale_policy_request_cannot_receive_a_changes_decision(self):
        submission, version, review, approval = self.create_request()
        _, draft = create_source(
            workspace=self.workspace, owner=self.manager, title="Policy update after request",
            source_type=BrandSource.SourceType.POLICY, description="Changes policy snapshot",
            source_text="New policy is now active.",
        )
        approve_source_version(source_id=draft.source_id, version_number=draft.version_number, workspace=self.workspace, actor=self.manager)
        with self.assertRaisesMessage(ValidationError, "brand policy changed"):
            request_changes(request_id=approval.pk, workspace=self.workspace, actor=self.reviewer, comment="Please update this after the policy changed.")
        self.assertFalse(ApprovalDecision.objects.filter(approval_request=approval).exists())

    def test_request_and_decision_are_workspace_scoped(self):
        submission, version, review, approval = self.create_request()
        other = type(self.workspace).objects.create(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
        with self.assertRaises(ValidationError):
            approve_version(request_id=approval.pk, workspace=other, actor=self.reviewer)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class RevisionAndApprovalViewTests(ReviewFixtureMixin, TestCase):
    def setup_client(self, user):
        self.client.force_login(user)

    def request_with_review(self, text="Luma Desk is the opening line."):
        submission, version, review = self.run_review(text)
        approval, _ = submit_for_approval(submission_id=submission.pk, workspace=self.workspace, actor=self.creator, review_run_id=review.pk)
        return submission, version, review, approval

    def test_creator_revision_view_appends_version_and_rejects_stale_post(self):
        submission, version, review = self.run_review("Original line.")
        self.setup_client(self.creator)
        url = reverse("app:content_reviews:revise", args=[submission.pk])
        get_response = self.client.get(url)
        self.assertEqual(get_response.status_code, 200)
        post = {
            "content_text": "Edited line.", "base_version_id": str(version.pk),
            "base_content_hash": version.content_hash, "idempotency_key": str(uuid.uuid4()),
        }
        response = self.client.post(url, post)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(submission.versions.count(), 2)
        stale = dict(post, idempotency_key=str(uuid.uuid4()), content_text="A competing stale edit.")
        response = self.client.post(url, stale)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "changed in another session")
        self.assertEqual(submission.versions.count(), 2)

    def test_version_comparison_uses_escaped_saved_text(self):
        submission, first, _ = self.run_review("<script>alert(1)</script>\nOld phrase.")
        _, second, _ = create_revision(
            submission_id=submission.pk, workspace=self.workspace, actor=self.creator,
            base_version_id=first.pk, base_content_hash=first.content_hash,
            content_text="<script>alert(1)</script>\nNew phrase.", idempotency_key=uuid.uuid4(),
        )
        self.setup_client(self.creator)
        response = self.client.get(reverse("app:content_reviews:compare", args=[submission.pk]) + f"?from=1&to={second.version_number}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Old phrase.")
        self.assertContains(response, "New phrase.")
        self.assertNotContains(response, "<script>alert(1)</script>")

    def test_non_owner_cannot_revise_creator_submission(self):
        submission, version, _ = self.run_review("Private creator text.")
        self.setup_client(self.other_creator)
        self.assertEqual(self.client.get(reverse("app:content_reviews:revise", args=[submission.pk])).status_code, 404)

    def test_creator_can_see_request_changes_decision(self):
        submission, version, review, approval = self.request_with_review()
        request_changes(request_id=approval.pk, workspace=self.workspace, actor=self.reviewer, comment="Please clarify the audience statement.")
        self.setup_client(self.creator)
        response = self.client.get(reverse("app:content_reviews:version_detail", args=[submission.pk, version.version_number]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please clarify the audience statement.")
        self.assertContains(response, "Changes requested")

    def test_reviewer_inbox_has_pending_real_request_and_creator_is_denied(self):
        submission, version, review, approval = self.request_with_review()
        self.setup_client(self.reviewer)
        response = self.client.get(reverse("app:approvals:inbox"))
        self.assertContains(response, submission.title)
        self.assertContains(response, "Instagram")
        self.setup_client(self.creator)
        self.assertEqual(self.client.get(reverse("app:approvals:inbox")).status_code, 403)

    def test_manager_cannot_open_approval_inbox_or_decide(self):
        submission, version, review, approval = self.request_with_review()
        self.setup_client(self.manager)
        self.assertEqual(self.client.get(reverse("app:approvals:inbox")).status_code, 403)
        self.assertEqual(self.client.post(reverse("app:approvals:detail", args=[approval.pk]), {"approve_version": "1"}).status_code, 403)

    def test_approval_end_to_end_real_engine_revision_evidence_and_current_approval(self):
        client = Client()
        client.force_login(self.creator)
        created = client.post(reverse("app:content_reviews:create"), {
            "title": "LumaDesk spring campaign", "channel": ContentSubmission.Channel.INSTAGRAM,
            "audience": "Operations teams", "campaign_purpose": "Product launch",
            "content_text": "Luma Desk gives teams a clearer start.",
        })
        self.assertEqual(created.status_code, 302)
        submission = ContentSubmission.objects.get(title="LumaDesk spring campaign", creator=self.creator)
        first = submission.current_version
        detail_url = reverse("app:content_reviews:version_detail", args=[submission.pk, first.version_number])
        detail = client.get(detail_url)
        token = hidden_value(detail, "idempotency_key")
        run_response = client.post(reverse("app:content_reviews:run", args=[submission.pk, first.version_number]), {"idempotency_key": token})
        self.assertEqual(run_response.status_code, 302)
        first_review = first.review_runs.get()
        self.assertTrue(first_review.findings.filter(rule__identifier="TERM-001").exists())

        revise_url = reverse("app:content_reviews:revise", args=[submission.pk])
        revise_page = client.get(revise_url)
        revised = client.post(revise_url, {
            "content_text": "Luma Desk gives teams a clearer start. Results vary.",
            "base_version_id": str(first.pk), "base_content_hash": first.content_hash,
            "idempotency_key": hidden_value(revise_page, "idempotency_key"),
        })
        self.assertEqual(revised.status_code, 302)
        second = submission.versions.get(version_number=2)
        self.assertEqual(second.review_runs.count(), 0)

        detail_url = reverse("app:content_reviews:version_detail", args=[submission.pk, second.version_number])
        detail = client.get(detail_url)
        run_response = client.post(reverse("app:content_reviews:run", args=[submission.pk, second.version_number]), {"idempotency_key": hidden_value(detail, "idempotency_key")})
        self.assertEqual(run_response.status_code, 302)
        second_review = second.review_runs.get()
        self.assertTrue(second_review.findings.filter(rule__identifier="TERM-001").exists())
        version_page = client.get(detail_url)
        submitted = client.post(reverse("app:content_reviews:submit", args=[submission.pk]), {"review_run_id": str(second_review.pk)})
        self.assertEqual(submitted.status_code, 302)
        approval = ApprovalRequest.objects.get(content_version=second)

        reviewer_client = Client()
        reviewer_client.force_login(self.reviewer)
        self.assertContains(reviewer_client.get(reverse("app:approvals:inbox")), submission.title)
        approval_detail = reviewer_client.get(reverse("app:approvals:detail", args=[approval.pk]))
        self.assertContains(approval_detail, "Do not write Luma Desk as two words.")
        self.assertContains(approval_detail, "Luma Desk")
        approved = reviewer_client.post(reverse("app:approvals:detail", args=[approval.pk]), {
            "approve_version": "1",
            "override_reason": "Reviewed the naming issue and accepted this campaign for this specific channel.",
        })
        self.assertEqual(approved.status_code, 302)
        submission.refresh_from_db()
        self.assertTrue(current_valid_approval(submission)["valid"])
        creator_page = client.get(detail_url)
        self.assertContains(creator_page, "Currently approved")
        self.assertTrue(first_review.findings.filter(rule__identifier="TERM-001").exists())

    def test_decision_endpoint_is_post_only_and_csrf_protected(self):
        submission, version, review, approval = self.request_with_review(text="LumaDesk is clear.")
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.reviewer)
        url = reverse("app:approvals:detail", args=[approval.pk])
        self.assertEqual(csrf_client.get(url).status_code, 200)
        self.assertEqual(csrf_client.post(url, {"approve_version": "1"}).status_code, 403)
        self.assertEqual(csrf_client.get(url, {"approve_version": "1"}).status_code, 200)
        self.assertFalse(ApprovalDecision.objects.filter(approval_request_id=approval.pk).exists())
