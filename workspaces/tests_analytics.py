import secrets
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Count
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from approvals.models import ApprovalDecision, ApprovalRequest
from approvals.services import approve_version, request_changes, submit_for_approval
from content_reviews.collaboration import change_finding_resolution
from content_reviews.models import (
    ContentSubmission,
    ContentVersion,
    FindingResolutionEvent,
    ReviewFinding,
    ReviewRun,
)
from content_reviews.tests import ReviewFixtureMixin
from workspaces.models import Workspace, WorkspaceMembership


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class WorkspaceAnalyticsTests(ReviewFixtureMixin, TestCase):
    def test_empty_analytics_explains_how_to_start(self):
        self.client.force_login(self.creator)
        response = self.client.get(reverse("app:analytics"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No review data in this activity window.")
        self.assertEqual(response.context["analytics"]["next_action_url"], reverse("app:content_reviews:create"))
        self.assertContains(response, "Create a content submission")
        self.assertEqual(response.context["analytics"]["review_count"], 0)
        self.assertEqual(response.context["analytics"]["submission_count"], 0)
        self.assertEqual(response.context["analytics"]["finding_count"], 0)
        self.assertEqual(response.context["analytics"]["approval_request_count"], 0)
        self.assertEqual(response.context["analytics"]["decision_count"], 0)
        self.assertEqual(response.context["analytics"]["classification_total"], 0)
        self.assertEqual(response.context["analytics"]["resolution_event_count"], 0)
        self.assertEqual(
            {item["key"] for item in response.context["analytics"]["resolution_states"]},
            {key for key, _label in ReviewFinding.ResolutionStatus.choices},
        )
        self.assertTrue(all(item["count"] == 0 for item in response.context["analytics"]["resolution_states"]))
        self.assertIsNone(response.context["analytics"]["median_decision_time"])
        self.assertContains(response, "No completed decisions yet")

    def test_creator_analytics_counts_only_their_persisted_reviews(self):
        _, _, creator_run = self.run_review("Meet Luma Desk today.", creator=self.creator)
        _, _, other_run = self.run_review("Meet Luma Desk tomorrow.", creator=self.other_creator)

        self.client.force_login(self.creator)
        response = self.client.get(reverse("app:analytics"))

        self.assertEqual(response.context["analytics"]["review_count"], 1)
        self.assertEqual(response.context["analytics"]["submission_count"], 1)
        self.assertEqual(response.context["analytics"]["finding_count"], creator_run.findings.count())
        self.assertEqual(response.context["analytics"]["scope_label"], "Your submissions")
        self.assertNotEqual(creator_run.pk, other_run.pk)

    def test_review_findings_classifications_categories_and_top_rules_are_real_aggregates(self):
        _, _, first_run = self.run_review("Meet Luma Desk today.", creator=self.creator)
        _, _, second_run = self.run_review("Meet Luma Desk tomorrow.", creator=self.creator)
        self.client.force_login(self.reviewer)

        response = self.client.get(reverse("app:analytics") + "?period=all")
        analytics = response.context["analytics"]
        expected_findings = ReviewFinding.objects.filter(
            review_run__in=[first_run, second_run], origin=ReviewFinding.Origin.DETERMINISTIC
        )

        self.assertEqual(analytics["review_count"], 2)
        self.assertEqual(analytics["completed_review_count"], 2)
        self.assertEqual(analytics["finding_count"], expected_findings.count())
        self.assertEqual(analytics["classification_total"], expected_findings.exclude(classification="").count())
        expected_classifications = dict(
            expected_findings.exclude(classification="").values("classification")
            .annotate(total=Count("pk")).values_list("classification", "total")
        )
        self.assertEqual(
            {row["key"]: row["count"] for row in analytics["classifications"]},
            expected_classifications,
        )
        self.assertEqual(sum(row["count"] for row in analytics["categories"]), analytics["finding_count"])
        self.assertTrue(all(0 <= row["proportion"] <= 100 for row in analytics["classifications"]))
        self.assertTrue(all(0 <= row["proportion"] <= 100 for row in analytics["categories"]))
        self.assertEqual(next(rule["count"] for rule in analytics["rules"] if rule["identifier"] == "TERM-001"), 2)

    def test_ai_origin_findings_are_not_mixed_into_deterministic_analytics(self):
        _, _, run = self.run_review("Meet Luma Desk today.", creator=self.creator)
        finding = run.findings.first()
        if finding:
            ReviewFinding.objects.filter(pk=finding.pk).update(origin=ReviewFinding.Origin.FUTURE_AI)
        self.client.force_login(self.reviewer)
        analytics = self.client.get(reverse("app:analytics") + "?period=all").context["analytics"]
        expected = ReviewFinding.objects.filter(
            review_run=run, origin=ReviewFinding.Origin.DETERMINISTIC
        ).count()
        self.assertEqual(analytics["finding_count"], expected)

    def test_resolution_state_and_event_analytics_use_phase_7b_records(self):
        _, version, run = self.run_review("Meet Luma Desk today.", creator=self.creator)
        finding = run.findings.first()
        self.assertIsNotNone(finding)
        if finding is None:
            return
        change_finding_resolution(
            workspace=self.workspace,
            actor=self.reviewer,
            submission_id=version.submission_id,
            version_number=version.version_number,
            finding_id=finding.pk,
            expected_status=ReviewFinding.ResolutionStatus.OPEN,
            new_status=ReviewFinding.ResolutionStatus.ACKNOWLEDGED,
            idempotency_key=uuid.uuid4(),
        )
        self.client.force_login(self.reviewer)
        analytics = self.client.get(reverse("app:analytics") + "?period=all").context["analytics"]
        states = {item["key"]: item["count"] for item in analytics["resolution_states"]}
        self.assertEqual(states[ReviewFinding.ResolutionStatus.ACKNOWLEDGED], 1)
        self.assertEqual(analytics["resolution_event_count"], FindingResolutionEvent.objects.count())
        self.assertEqual(analytics["resolution_event_count"], 1)

    def test_approval_requests_decisions_and_median_turnaround_are_persisted(self):
        first_submission, _, first_run = self.run_review(
            "LumaDesk helps teams plan their week.", creator=self.creator
        )
        first_request, _ = submit_for_approval(
            submission_id=first_submission.pk,
            workspace=self.workspace,
            actor=self.creator,
            review_run_id=first_run.pk,
        )
        first_decision = approve_version(
            request_id=first_request.pk, workspace=self.workspace, actor=self.reviewer
        )
        ApprovalRequest.objects.filter(pk=first_request.pk).update(
            requested_at=first_decision.decided_at - timedelta(hours=2)
        )

        second_submission, _, second_run = self.run_review(
            "LumaDesk supports an easier weekly plan.", creator=self.creator
        )
        second_request, _ = submit_for_approval(
            submission_id=second_submission.pk,
            workspace=self.workspace,
            actor=self.creator,
            review_run_id=second_run.pk,
        )
        second_decision = request_changes(
            request_id=second_request.pk,
            workspace=self.workspace,
            actor=self.reviewer,
            comment="Please clarify the audience before this is approved.",
        )
        ApprovalRequest.objects.filter(pk=second_request.pk).update(
            requested_at=second_decision.decided_at - timedelta(hours=4)
        )

        self.client.force_login(self.reviewer)
        analytics = self.client.get(reverse("app:analytics") + "?period=all").context["analytics"]
        request_states = {item["key"]: item["count"] for item in analytics["request_states"]}
        decisions = {item["key"]: item["count"] for item in analytics["decisions"]}
        self.assertEqual(analytics["approval_request_count"], 2)
        self.assertEqual(analytics["decision_count"], 2)
        self.assertEqual(analytics["approved_decision_count"], 1)
        self.assertEqual(request_states[ApprovalRequest.Status.APPROVED], 1)
        self.assertEqual(request_states[ApprovalRequest.Status.CHANGES_REQUESTED], 1)
        self.assertEqual(decisions[ApprovalDecision.Action.APPROVED], 1)
        self.assertEqual(decisions[ApprovalDecision.Action.CHANGES_REQUESTED], 1)
        self.assertEqual(analytics["median_decision_seconds"], 3 * 60 * 60)
        self.assertEqual(analytics["median_decision_time"], "3h")

    def test_invalid_time_window_falls_back_to_last_30_days(self):
        self.client.force_login(self.creator)
        response = self.client.get(reverse("app:analytics") + "?period=forever")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["analytics"]["period"], "30d")
        self.assertContains(response, "Last 30 days")

    def test_time_filter_and_activity_chart_use_saved_review_timestamps(self):
        _, _, run = self.run_review("Meet Luma Desk today.", creator=self.creator)
        old = timezone.now() - timedelta(days=45)
        ReviewRun.objects.filter(pk=run.pk).update(started_at=old)
        ContentSubmission.objects.filter(pk=run.content_version.submission_id).update(
            created_at=old, updated_at=old
        )
        self.client.force_login(self.creator)
        recent = self.client.get(reverse("app:analytics") + "?period=7d").context["analytics"]
        all_time = self.client.get(reverse("app:analytics") + "?period=all").context["analytics"]
        self.assertEqual(recent["review_count"], 0)
        self.assertEqual(recent["submission_count"], 0)
        self.assertEqual(recent["activity"]["count"], 0)
        self.assertEqual(all_time["review_count"], 1)
        self.assertEqual(all_time["activity"]["label"], "Review starts · last 8 weeks")

    def test_reviewer_analytics_aggregates_current_workspace_records(self):
        self.run_review("Meet Luma Desk today.", creator=self.creator)
        self.run_review("Meet Luma Desk tomorrow.", creator=self.other_creator)

        self.client.force_login(self.reviewer)
        response = self.client.get(reverse("app:analytics"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["analytics"]["review_count"], 2)
        self.assertEqual(response.context["analytics"]["scope_label"], "Current workspace")
        self.assertTrue(response.context["analytics"]["rules"])

    def test_brand_manager_can_view_workspace_analytics(self):
        self.run_review("Meet Luma Desk today.", creator=self.creator)
        self.client.force_login(self.manager)
        response = self.client.get(reverse("app:analytics"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["analytics"]["review_count"], 1)
        self.assertEqual(response.context["analytics"]["scope_label"], "Current workspace")

    def test_workspace_admin_can_view_workspace_analytics(self):
        self.run_review("Meet Luma Desk today.", creator=self.creator)
        admin = get_user_model().objects.create_user(
            username=f"admin-{uuid.uuid4().hex[:8]}", password=secrets.token_urlsafe(24)
        )
        WorkspaceMembership.objects.create(
            user=admin, workspace=self.workspace, role=WorkspaceMembership.Role.ADMIN
        )
        self.client.force_login(admin)
        response = self.client.get(reverse("app:analytics"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["analytics"]["review_count"], 1)
        self.assertEqual(response.context["analytics"]["scope_label"], "Current workspace")

    def test_other_workspace_records_are_excluded(self):
        self.run_review("Meet Luma Desk in this workspace.", creator=self.creator)
        other_workspace = Workspace.objects.create(name="Separate", slug=f"separate-{uuid.uuid4().hex[:8]}")
        remote_creator = get_user_model().objects.create_user(
            username=f"remote-{uuid.uuid4().hex[:8]}", password=secrets.token_urlsafe(24)
        )
        WorkspaceMembership.objects.create(
            user=remote_creator, workspace=other_workspace, role=WorkspaceMembership.Role.CREATOR
        )
        submission = ContentSubmission.objects.create(
            workspace=other_workspace,
            creator=remote_creator,
            title="Private other-workspace item",
            channel=ContentSubmission.Channel.EMAIL,
        )
        version = ContentVersion.objects.create(
            submission=submission,
            version_number=1,
            content_text="Private synthetic content.",
            created_by=remote_creator,
        )
        submission.current_version = version
        submission.save(update_fields=["current_version"])
        now = timezone.now()
        ReviewRun.objects.create(
            workspace=other_workspace,
            content_version=version,
            requested_by=remote_creator,
            status=ReviewRun.Status.COMPLETE,
            started_at=now - timedelta(days=1),
            completed_at=now,
            engine_version="analytics-isolation-test",
        )

        self.client.force_login(self.reviewer)
        response = self.client.get(reverse("app:analytics"))

        self.assertEqual(response.context["analytics"]["review_count"], 1)
        self.assertNotContains(response, "Private other-workspace item")

    def test_anonymous_user_cannot_open_analytics(self):
        response = Client().get(reverse("app:analytics"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)
