import uuid
from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from approvals.models import ApprovalRequest
from approvals.services import approve_version, request_changes, submit_for_approval
from content_reviews.services import create_submission, execute_review
from content_reviews.tests import ReviewFixtureMixin
from workspaces.models import Workspace, WorkspaceMembership


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ApprovalInboxFilterTests(ReviewFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("app:approvals:inbox")

    def create_request(self, title, *, creator=None, text="LumaDesk is useful."):
        creator = creator or self.creator
        submission, version = self.create_submission_for(creator=creator, title=title, text=text)
        review = execute_review(
            submission_id=submission.pk,
            version_number=version.version_number,
            workspace=self.workspace,
            actor=creator,
            idempotency_key=uuid.uuid4(),
        )
        approval, _created = submit_for_approval(
            submission_id=submission.pk,
            workspace=self.workspace,
            actor=creator,
            review_run_id=review.pk,
        )
        return approval

    def get_inbox(self, query=""):
        self.client.force_login(self.reviewer)
        return self.client.get(f"{self.url}{query}")

    def test_default_inbox_shows_pending_requests_newest_first(self):
        older = self.create_request("Older campaign")
        newer = self.create_request("Newer campaign")
        ApprovalRequest.objects.filter(pk=older.pk).update(requested_at=timezone.now() - timedelta(days=1))
        ApprovalRequest.objects.filter(pk=newer.pk).update(requested_at=timezone.now())

        response = self.get_inbox()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_filter"], "pending")
        self.assertEqual([row["approval"].pk for row in response.context["rows"]], [newer.pk, older.pk])
        self.assertContains(response, "Find approval requests that need your attention")
        self.assertContains(response, "aria-current=\"page\"")

    def test_pending_filter_uses_existing_in_review_status(self):
        pending = self.create_request("Waiting campaign")
        changed = self.create_request("Changed campaign")
        request_changes(request_id=changed.pk, workspace=self.workspace, actor=self.reviewer, comment="Please clarify this campaign copy.")

        response = self.get_inbox("?status=pending")

        self.assertEqual([row["approval"].pk for row in response.context["rows"]], [pending.pk])

    def test_changes_requested_filter_uses_existing_status(self):
        pending = self.create_request("Waiting campaign")
        changed = self.create_request("Changed campaign")
        request_changes(request_id=changed.pk, workspace=self.workspace, actor=self.reviewer, comment="Please clarify this campaign copy.")

        response = self.get_inbox("?status=changes_requested")

        self.assertEqual([row["approval"].pk for row in response.context["rows"]], [changed.pk])
        self.assertNotIn(pending.pk, [row["approval"].pk for row in response.context["rows"]])

    def test_approved_filter_uses_existing_status(self):
        pending = self.create_request("Waiting campaign")
        approved = self.create_request("Approved campaign")
        approve_version(request_id=approved.pk, workspace=self.workspace, actor=self.reviewer)

        response = self.get_inbox("?status=approved")

        self.assertEqual([row["approval"].pk for row in response.context["rows"]], [approved.pk])
        self.assertNotIn(pending.pk, [row["approval"].pk for row in response.context["rows"]])

    def test_all_filter_prioritizes_pending_then_changes_requested_then_approved(self):
        approved = self.create_request("Approved campaign")
        changed = self.create_request("Changed campaign")
        pending = self.create_request("Waiting campaign")
        approve_version(request_id=approved.pk, workspace=self.workspace, actor=self.reviewer)
        request_changes(request_id=changed.pk, workspace=self.workspace, actor=self.reviewer, comment="Please clarify this campaign copy.")

        response = self.get_inbox("?status=all")

        self.assertEqual(
            [row["approval"].pk for row in response.context["rows"]],
            [pending.pk, changed.pk, approved.pk],
        )

    def test_invalid_status_falls_back_to_pending(self):
        pending = self.create_request("Waiting campaign")
        changed = self.create_request("Changed campaign")
        request_changes(request_id=changed.pk, workspace=self.workspace, actor=self.reviewer, comment="Please clarify this campaign copy.")

        response = self.get_inbox("?status=unexpected")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_filter"], "pending")
        self.assertEqual([row["approval"].pk for row in response.context["rows"]], [pending.pk])

    def test_search_matches_creator_display_name(self):
        self.creator.first_name = "Ada"
        self.creator.last_name = "Lovelace"
        self.creator.save(update_fields=["first_name", "last_name"])
        request = self.create_request("Spring launch")

        response = self.get_inbox("?q=Ada%20Lovelace")

        self.assertEqual([row["approval"].pk for row in response.context["rows"]], [request.pk])

    def test_search_matches_creator_username(self):
        request = self.create_request("Spring launch")

        response = self.get_inbox("?q=creator")

        self.assertEqual([row["approval"].pk for row in response.context["rows"]], [request.pk])

    def test_search_matches_submission_title(self):
        matching = self.create_request("Spring campaign")
        self.create_request("Autumn announcement")

        response = self.get_inbox("?q=spring")

        self.assertEqual([row["approval"].pk for row in response.context["rows"]], [matching.pk])

    def test_no_result_search_has_contextual_empty_state_and_clear_link(self):
        self.create_request("Spring campaign")

        response = self.get_inbox("?q=does-not-match")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No review requests match")
        self.assertContains(response, "Clear filters")

    def test_whitespace_search_is_treated_as_blank(self):
        request = self.create_request("Spring campaign")

        response = self.get_inbox("?q=%20%20%20")

        self.assertEqual(response.context["query"], "")
        self.assertEqual([row["approval"].pk for row in response.context["rows"]], [request.pk])

    def test_long_search_is_bounded_and_does_not_error(self):
        response = self.get_inbox("?q=" + ("x" * 500))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["query"]), 120)

    def test_status_and_search_combine_and_filter_links_preserve_search(self):
        changed = self.create_request("Spring campaign")
        request_changes(request_id=changed.pk, workspace=self.workspace, actor=self.reviewer, comment="Please clarify this campaign copy.")
        self.create_request("Spring pending")

        response = self.get_inbox("?status=changes_requested&q=spring")

        self.assertEqual([row["approval"].pk for row in response.context["rows"]], [changed.pk])
        self.assertContains(response, "status=all&amp;q=spring")

    def test_workspace_scope_is_preserved_during_search(self):
        own = self.create_request("Visible campaign")
        other = Workspace.objects.create(name="Other workspace", slug=f"other-{uuid.uuid4().hex[:8]}")
        other_manager = self.make_user("other_manager")
        other_creator = self.make_user("other_creator")
        WorkspaceMembership.objects.create(user=other_manager, workspace=other, role=WorkspaceMembership.Role.BRAND_MANAGER)
        WorkspaceMembership.objects.create(user=other_creator, workspace=other, role=WorkspaceMembership.Role.CREATOR)
        self.create_policy(workspace=other)
        submission, version = create_submission(
            workspace=other, creator=other_creator, title="Secret campaign",
            channel="EMAIL", audience="Internal", campaign_purpose="Test",
            content_text="LumaDesk is useful.",
        )
        review = execute_review(
            submission_id=submission.pk, version_number=version.version_number,
            workspace=other, actor=other_creator, idempotency_key=uuid.uuid4(),
        )
        submit_for_approval(
            submission_id=submission.pk, workspace=other, actor=other_creator,
            review_run_id=review.pk,
        )

        response = self.get_inbox("?status=all&q=campaign")

        ids = [row["approval"].pk for row in response.context["rows"]]
        self.assertEqual(ids, [own.pk])
        self.assertNotContains(response, "Secret campaign")

    def test_creator_cannot_access_reviewer_inbox(self):
        self.client.force_login(self.creator)

        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_inactive_membership_cannot_access_reviewer_inbox(self):
        membership = WorkspaceMembership.objects.get(workspace=self.workspace, user=self.reviewer)
        membership.is_active = False
        membership.save(update_fields=["is_active"])
        self.client.force_login(self.reviewer)

        self.assertEqual(self.client.get(self.url).status_code, 403)
