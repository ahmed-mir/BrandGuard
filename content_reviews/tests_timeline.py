import uuid
from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ai_analysis.models import AIAnalysisRun
from approvals.services import approve_version, request_changes, submit_for_approval
from brand_sources.models import BrandSource
from brand_sources.services import approve_source_version, create_source, policy_snapshot
from content_reviews.collaboration import add_review_comment, change_finding_resolution
from content_reviews.models import ContentSubmission, ContentVersion, ReviewFinding, ReviewRun
from content_reviews.services import create_revision
from content_reviews.timeline import build_submission_timeline
from workspaces.models import WorkspaceMembership

from .tests import ReviewFixtureMixin


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SubmissionTimelineTests(ReviewFixtureMixin, TestCase):
    def timeline(self, submission):
        return build_submission_timeline(workspace=self.workspace, submission=submission)

    def create_approval(self, text="LumaDesk is clear."):
        submission, version, review = self.run_review(text)
        approval, _created = submit_for_approval(
            submission_id=submission.pk,
            workspace=self.workspace,
            actor=self.creator,
            review_run_id=review.pk,
        )
        return submission, version, review, approval

    def test_submission_and_multiple_version_events_include_authors_and_version_labels(self):
        submission, first = self.create_submission_for(text="Original text.")
        _, second, _created = create_revision(
            submission_id=submission.pk, workspace=self.workspace, actor=self.creator,
            base_version_id=first.pk, base_content_hash=first.content_hash,
            content_text="Revised text.", idempotency_key=uuid.uuid4(),
        )

        events = self.timeline(submission)["events"]

        self.assertEqual(events[0]["title"], "Version 2 created")
        self.assertEqual(events[0]["version_number"], 2)
        self.assertEqual(events[0]["actor"], self.creator)
        self.assertIn("Version 1 created", [event["title"] for event in events])
        self.assertIn("Submission created", [event["title"] for event in events])
        self.assertEqual(second.version_number, 2)
        self.assertEqual([event["timestamp"] for event in events], sorted((event["timestamp"] for event in events), reverse=True))

    def test_completed_deterministic_review_is_summarized_without_run_id(self):
        submission, version, run = self.run_review("Meet Luma Desk today.")

        events = self.timeline(submission)["events"]
        review_event = next(event for event in events if event["event_type"] == "review")

        self.assertEqual(review_event["title"], "Policy review completed")
        self.assertEqual(review_event["version_number"], version.version_number)
        self.assertIn("1 finding", review_event["description"])
        self.assertNotIn(str(run.pk), str(review_event))

    def test_general_and_finding_specific_comments_are_distinguished_and_versioned(self):
        submission, version, run = self.run_review("Meet Luma Desk today.")
        finding = run.findings.get()
        add_review_comment(
            workspace=self.workspace, actor=self.creator, submission_id=submission.pk,
            version_number=version.version_number, body="General review note.", idempotency_key=uuid.uuid4(),
        )
        add_review_comment(
            workspace=self.workspace, actor=self.reviewer, submission_id=submission.pk,
            version_number=version.version_number, finding_id=finding.pk,
            body="Please correct the product spelling.", idempotency_key=uuid.uuid4(),
        )

        events = self.timeline(submission)["events"]
        comments = [event for event in events if event["event_type"] == "comment"]

        self.assertEqual(len(comments), 2)
        self.assertEqual({event["version_number"] for event in comments}, {version.version_number})
        self.assertTrue(any(event["title"] == "Review discussion comment added" for event in comments))
        self.assertTrue(any("finding" in event["title"] for event in comments))
        self.assertTrue(any(event["role"] == "Reviewer" for event in comments))

    def test_resolution_event_is_visible_on_original_version_after_revision(self):
        submission, version, run = self.run_review("Meet Luma Desk today.")
        finding = run.findings.get()
        change_finding_resolution(
            workspace=self.workspace, actor=self.reviewer, submission_id=submission.pk,
            version_number=version.version_number, finding_id=finding.pk,
            expected_status=ReviewFinding.ResolutionStatus.OPEN,
            new_status=ReviewFinding.ResolutionStatus.ACKNOWLEDGED,
            idempotency_key=uuid.uuid4(),
        )
        _, second, _ = create_revision(
            submission_id=submission.pk, workspace=self.workspace, actor=self.creator,
            base_version_id=version.pk, base_content_hash=version.content_hash,
            content_text="Meet LumaDesk today.", idempotency_key=uuid.uuid4(),
        )

        events = self.timeline(submission)["events"]
        resolution = next(event for event in events if event["event_type"] == "resolution")

        self.assertEqual(resolution["title"], "Finding acknowledged")
        self.assertEqual(resolution["version_number"], version.version_number)
        self.assertNotEqual(resolution["version_number"], second.version_number)
        self.assertEqual(resolution["actor"], self.reviewer)

    def test_approval_request_and_approved_decision_events_are_distinct(self):
        submission, version, _review, approval = self.create_approval()
        approve_version(request_id=approval.pk, workspace=self.workspace, actor=self.reviewer)

        events = self.timeline(submission)["events"]
        approval_events = [event for event in events if event["event_type"].startswith("approval-")]

        self.assertEqual({event["title"] for event in approval_events}, {"Approval requested", "Version approved"})
        self.assertTrue(all(event["version_number"] == version.version_number for event in approval_events))
        decision_event = next(event for event in approval_events if event["event_type"] == "approval-decision")
        self.assertEqual(decision_event["actor"], self.reviewer)

    def test_changes_requested_event_includes_a_short_safe_summary(self):
        submission, _version, _review, approval = self.create_approval()
        request_changes(
            request_id=approval.pk, workspace=self.workspace, actor=self.reviewer,
            comment="Please clarify the intended audience before approval.",
        )

        event = next(event for event in self.timeline(submission)["events"] if event["event_type"] == "approval-decision")

        self.assertEqual(event["title"], "Changes requested")
        self.assertEqual(event["description"], "Please clarify the intended audience before approval.")
        self.assertEqual(event["actor"], self.reviewer)

    def test_long_comment_is_truncated_and_plain_text(self):
        submission, version = self.create_submission_for(text="A saved draft.")
        body = "Words & <safe> " * 200
        add_review_comment(
            workspace=self.workspace, actor=self.creator, submission_id=submission.pk,
            version_number=version.version_number, body=body, idempotency_key=uuid.uuid4(),
        )

        event = next(event for event in self.timeline(submission)["events"] if event["event_type"] == "comment")

        self.assertLessEqual(len(event["description"]), 181)
        self.assertIn("<safe>", event["description"])

    def test_demo_ai_event_is_unambiguously_simulated(self):
        submission, version = self.create_submission_for(text="LumaDesk is useful.")
        self.workspace.refresh_from_db()
        snapshot = policy_snapshot(self.workspace)
        AIAnalysisRun.objects.create(
            workspace=self.workspace,
            content_version=version,
            requested_by=self.creator,
            content_hash=version.content_hash,
            policy_revision=snapshot["revision"],
            policy_snapshot_hash=snapshot["hash"],
            policy_snapshot=snapshot,
            provider="demo",
            model_identifier="simulated-brandguard-demo",
            status=AIAnalysisRun.Status.COMPLETE,
            completed_at=timezone.now(),
        )

        event = next(event for event in self.timeline(submission)["events"] if event["event_type"] == "demo-ai")

        self.assertIn("simulated", event["title"].lower())
        self.assertIn("No external AI provider was contacted", event["description"])
        self.assertIn("not an approval decision", event["description"])

    def test_stale_approval_is_a_current_marker_without_a_fabricated_timestamp(self):
        submission, version, _review, approval = self.create_approval()
        approve_version(request_id=approval.pk, workspace=self.workspace, actor=self.reviewer)
        create_revision(
            submission_id=submission.pk, workspace=self.workspace, actor=self.creator,
            base_version_id=version.pk, base_content_hash=version.content_hash,
            content_text="Newer content version.", idempotency_key=uuid.uuid4(),
        )

        stale = self.timeline(submission)["stale_approval"]

        self.assertEqual(stale["title"], "Approval is no longer current")
        self.assertIn("content changed", stale["reason"])
        self.assertNotIn("timestamp", stale)

    def test_policy_change_stale_approval_is_a_current_marker(self):
        submission, _version, _review, approval = self.create_approval()
        approve_version(request_id=approval.pk, workspace=self.workspace, actor=self.reviewer)
        source, draft = create_source(
            workspace=self.workspace, owner=self.manager, title="Updated approved guidance",
            source_type=BrandSource.SourceType.POLICY, description="Synthetic timeline test policy.",
            source_text="Keep claims precise and supportable.",
        )
        approve_source_version(
            source_id=source.pk, version_number=draft.version_number,
            workspace=self.workspace, actor=self.manager,
        )

        stale = self.timeline(submission)["stale_approval"]

        self.assertEqual(stale["title"], "Approval is no longer current")
        self.assertIn("policy changed", stale["reason"])
        self.assertNotIn("timestamp", stale)

    def test_empty_version_history_still_shows_submission_creation(self):
        submission = ContentSubmission.objects.create(
            workspace=self.workspace, creator=self.creator, title="Empty draft",
            channel=ContentSubmission.Channel.EMAIL,
        )

        timeline = self.timeline(submission)

        self.assertEqual(len(timeline["events"]), 1)
        self.assertEqual(timeline["events"][0]["title"], "Submission created")

    def test_workspace_scope_is_enforced_and_creator_cannot_open_another_submission(self):
        submission, version = self.create_submission_for(creator=self.other_creator, text="Private other creator content.")
        other_workspace = type(self.workspace).objects.create(name="Other workspace", slug=f"other-{uuid.uuid4().hex[:8]}")

        with self.assertRaises(PermissionDenied):
            build_submission_timeline(workspace=other_workspace, submission=submission)

        self.client.force_login(self.creator)
        url = reverse("app:content_reviews:version_detail", args=[submission.pk, version.version_number])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_version_detail_renders_timeline_for_authorized_workspace_reviewer(self):
        submission, version, _review = self.run_review("LumaDesk is useful.")
        self.client.force_login(self.reviewer)

        response = self.client.get(reverse("app:content_reviews:version_detail", args=[submission.pk, version.version_number]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Recent activity · newest first")
        self.assertContains(response, "Policy review completed")

    def test_timeline_build_does_not_mutate_lifecycle_records(self):
        submission, version, run = self.run_review("Meet Luma Desk today.")
        before = (
            ContentVersion.objects.filter(submission=submission).count(),
            ReviewRun.objects.filter(content_version=version).count(),
            submission.approval_requests.count(),
            run.findings.count(),
        )

        self.timeline(submission)

        self.assertEqual(before, (
            ContentVersion.objects.filter(submission=submission).count(),
            ReviewRun.objects.filter(content_version=version).count(),
            submission.approval_requests.count(),
            run.findings.count(),
        ))
