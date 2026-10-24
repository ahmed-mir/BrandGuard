import secrets
from unittest import mock
from html.parser import HTMLParser
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.management import call_command, CommandError
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.urls import resolve
from django.utils import timezone

from workspaces.models import Workspace, WorkspaceMembership
from workspaces.permissions import resolve_current_membership, workspace_role_required
from content_reviews.models import ContentSubmission, ContentVersion, ReviewRun


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class WorkspaceAccessTests(TestCase):
    def setUp(self):
        self.password = secrets.token_urlsafe(24)
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="creator_one",
            email="creator.one@example.test",
            password=self.password,
            first_name="Casey",
            last_name="Creator",
        )
        self.workspace = Workspace.objects.create(name="LumaDesk", slug="lumadesk-workspace-test")
        self.membership = WorkspaceMembership.objects.create(
            user=self.user,
            workspace=self.workspace,
            role=WorkspaceMembership.Role.CREATOR,
        )

    def test_current_membership_is_resolved_from_authenticated_user(self):
        resolved = resolve_current_membership(self.user)
        self.assertEqual(resolved.pk, self.membership.pk)
        self.assertEqual(resolved.workspace, self.workspace)

    def test_app_home_shows_actual_identity_workspace_and_membership_role(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("app:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Casey Creator")
        self.assertContains(response, "creator.one@example.test")
        self.assertContains(response, "LumaDesk")
        self.assertContains(response, "Creator")
        self.assertNotContains(response, "Compliance score")
        self.assertContains(response, "Recent submissions")
        self.assertContains(response, "Approved source versions")
        self.assertContains(response, "Latest policy reviews")
        self.assertContains(response, "Create a content submission")
        self.assertNotContains(response, "Compliance score")

    def test_overview_counts_real_submissions_and_scopes_creator_records(self):
        ContentSubmission.objects.create(
            workspace=self.workspace,
            creator=self.user,
            title="My synthetic draft",
            channel=ContentSubmission.Channel.EMAIL,
        )
        other_creator = get_user_model().objects.create_user(
            username="second_creator", password=secrets.token_urlsafe(24)
        )
        WorkspaceMembership.objects.create(
            user=other_creator, workspace=self.workspace, role=WorkspaceMembership.Role.CREATOR
        )
        ContentSubmission.objects.create(
            workspace=self.workspace,
            creator=other_creator,
            title="Another creator private draft",
            channel=ContentSubmission.Channel.EMAIL,
        )

        self.client.force_login(self.user)
        response = self.client.get(reverse("app:home"))

        self.assertEqual(response.context["submission_count"], 1)
        self.assertContains(response, "My synthetic draft")
        self.assertNotContains(response, "Another creator private draft")

    def test_reviewer_overview_shows_real_workspace_submissions(self):
        ContentSubmission.objects.create(
            workspace=self.workspace,
            creator=self.user,
            title="Creator draft visible to reviewer",
            channel=ContentSubmission.Channel.EMAIL,
        )
        reviewer = get_user_model().objects.create_user(
            username="workspace_reviewer", password=secrets.token_urlsafe(24)
        )
        WorkspaceMembership.objects.create(
            user=reviewer, workspace=self.workspace, role=WorkspaceMembership.Role.REVIEWER
        )
        self.client.force_login(reviewer)
        response = self.client.get(reverse("app:home"))
        self.assertEqual(response.context["submission_count"], 1)
        self.assertContains(response, "Creator draft visible to reviewer")

    def test_overview_links_to_a_persisted_policy_review(self):
        submission = ContentSubmission.objects.create(
            workspace=self.workspace,
            creator=self.user,
            title="A reviewed synthetic draft",
            channel=ContentSubmission.Channel.EMAIL,
        )
        version = ContentVersion.objects.create(
            submission=submission,
            version_number=1,
            content_text="Synthetic marketing copy for a dashboard test.",
            created_by=self.user,
        )
        submission.current_version = version
        submission.save(update_fields=["current_version"])
        now = timezone.now()
        run = ReviewRun.objects.create(
            workspace=self.workspace,
            content_version=version,
            requested_by=self.user,
            status=ReviewRun.Status.COMPLETE,
            started_at=now,
            completed_at=now,
            policy_revision=0,
            policy_snapshot={},
            engine_version="dashboard-test",
        )
        self.client.force_login(self.user)
        response = self.client.get(reverse("app:home"))
        self.assertContains(response, "A reviewed synthetic draft")
        self.assertContains(response, "Latest policy reviews")
        self.assertContains(
            response,
            reverse(
                "app:content_reviews:review_detail",
                args=[submission.pk, version.version_number, run.pk],
            ),
        )

    def test_each_membership_role_is_rendered_from_database(self):
        role_labels = {
            WorkspaceMembership.Role.CREATOR: "Creator",
            WorkspaceMembership.Role.BRAND_MANAGER: "Brand manager",
            WorkspaceMembership.Role.REVIEWER: "Reviewer",
            WorkspaceMembership.Role.ADMIN: "Workspace admin",
        }
        for index, (role, label) in enumerate(role_labels.items()):
            with self.subTest(role=role):
                user = get_user_model().objects.create_user(
                    username=f"role_user_{index}", password=secrets.token_urlsafe(24)
                )
                WorkspaceMembership.objects.create(
                    user=user, workspace=self.workspace, role=role
                )
                self.client.force_login(user)
                response = self.client.get(reverse("app:home"))
                self.assertContains(response, label)
                self.client.logout()

    def test_inactive_membership_is_denied(self):
        self.membership.is_active = False
        self.membership.save(update_fields=["is_active"])
        self.client.force_login(self.user)
        response = self.client.get(reverse("app:home"))
        self.assertEqual(response.status_code, 403)

    def test_inactive_workspace_is_denied(self):
        self.workspace.is_active = False
        self.workspace.save(update_fields=["is_active"])
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("app:home")).status_code, 403)

    def test_ambiguous_multiple_active_memberships_are_not_guessed(self):
        other = Workspace.objects.create(name="Second", slug="second-workspace-test")
        WorkspaceMembership.objects.create(
            user=self.user, workspace=other, role=WorkspaceMembership.Role.REVIEWER
        )
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("app:home")).status_code, 403)

    def test_client_cannot_change_role_or_workspace_through_app_request(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("app:home"),
            {"role": WorkspaceMembership.Role.ADMIN, "workspace_id": 99999},
        )
        self.assertEqual(response.status_code, 405)
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.role, WorkspaceMembership.Role.CREATOR)
        self.assertEqual(self.membership.workspace_id, self.workspace.pk)

    def test_workspace_role_gate_blocks_creator_from_admin_operation(self):
        target = self.membership

        def protected_membership_change(request):
            target.role = request.POST["role"]
            target.save(update_fields=["role"])
            return HttpResponse(status=204)

        guarded = workspace_role_required(WorkspaceMembership.Role.ADMIN)(
            protected_membership_change
        )
        request = RequestFactory().post(
            "/app/test-only-membership-change/",
            {"role": WorkspaceMembership.Role.ADMIN},
        )
        request.user = self.user
        with self.assertRaises(PermissionDenied):
            guarded(request)
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.role, WorkspaceMembership.Role.CREATOR)

    def test_role_gate_uses_admin_membership_for_authorized_access(self):
        admin_user = get_user_model().objects.create_user(
            username="workspace_admin", password=secrets.token_urlsafe(24)
        )
        WorkspaceMembership.objects.create(
            user=admin_user,
            workspace=self.workspace,
            role=WorkspaceMembership.Role.ADMIN,
        )

        def protected_view(request):
            return HttpResponse(request.workspace_membership.get_role_display())

        guarded = workspace_role_required(WorkspaceMembership.Role.ADMIN)(protected_view)
        request = RequestFactory().get("/app/test-only-admin-view/")
        request.user = admin_user
        response = guarded(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), "Workspace admin")

    def test_database_prevents_duplicate_user_workspace_memberships(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                WorkspaceMembership.objects.create(
                    user=self.user,
                    workspace=self.workspace,
                    role=WorkspaceMembership.Role.ADMIN,
                )


class NavigationAnchorParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_navigation = False
        self.unavailable_depth = 0
        self.unavailable_text = ""
        self.paths = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "nav":
            self.in_navigation = True
        if tag == "span" and "app-nav-unavailable" in attributes.get("class", ""):
            self.unavailable_depth += 1
        elif tag == "span" and self.unavailable_depth:
            self.unavailable_depth += 1
        if self.in_navigation and tag == "a" and attributes.get("href"):
            self.paths.append(urlsplit(attributes["href"]).path)

    def handle_data(self, data):
        if self.unavailable_depth:
            self.unavailable_text += data.strip() + " "

    def handle_endtag(self, tag):
        if tag == "nav":
            self.in_navigation = False
        if tag == "span" and self.unavailable_depth:
            self.unavailable_depth -= 1


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class WorkspaceExperienceTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="LumaDesk", slug="lumadesk-experience")

    def create_member(self, username, role, *, workspace=None, active=True):
        user = get_user_model().objects.create_user(
            username=username,
            email=f"{username}@example.test",
            password=secrets.token_urlsafe(24),
            first_name=username.replace("_", " ").title(),
        )
        WorkspaceMembership.objects.create(
            user=user,
            workspace=workspace or self.workspace,
            role=role,
            is_active=active,
        )
        return user

    def test_role_specific_overview_copy_and_navigation(self):
        expected = {
            WorkspaceMembership.Role.CREATOR: (
                "Submit campaign text",
                [],
            ),
            WorkspaceMembership.Role.BRAND_MANAGER: (
                "approved, evidence-backed guidelines",
                [],
            ),
            WorkspaceMembership.Role.REVIEWER: (
                "record a decision on the exact version submitted for review",
                [],
            ),
            WorkspaceMembership.Role.ADMIN: (
                "Oversee workspace access",
                [],
            ),
        }
        for index, (role, details) in enumerate(expected.items()):
            with self.subTest(role=role):
                user = self.create_member(f"nav_user_{index}", role)
                self.client.force_login(user)
                response = self.client.get(reverse("app:home"))
                self.assertContains(response, details[0])
                parser = NavigationAnchorParser()
                parser.feed(response.content.decode())
                expected_unavailable = [
                    item for label in details[1] for item in (*label.split(), "Unavailable")
                ]
                self.assertEqual(parser.unavailable_text.split(), expected_unavailable)
                expected_links = {reverse("app:home"), reverse("app:profile")}
                expected_links.add(reverse("app:brand_sources:list"))
                expected_links.add(reverse("app:content_reviews:list"))
                if role in {WorkspaceMembership.Role.REVIEWER, WorkspaceMembership.Role.ADMIN}:
                    expected_links.add(reverse("app:approvals:inbox"))
                if role == WorkspaceMembership.Role.ADMIN:
                    expected_links.add(reverse("app:members"))
                self.assertEqual(set(parser.paths), expected_links)
                self.client.logout()

    def test_navigation_links_resolve_and_future_items_are_not_links(self):
        creator = self.create_member("navigation_creator", WorkspaceMembership.Role.CREATOR)
        self.client.force_login(creator)
        response = self.client.get(reverse("app:home"))
        parser = NavigationAnchorParser()
        parser.feed(response.content.decode())
        self.assertEqual(
            set(parser.paths),
            {
                reverse("app:home"),
                reverse("app:profile"),
                reverse("app:brand_sources:list"),
                reverse("app:content_reviews:list"),
            },
        )
        for path in parser.paths:
            self.assertIsNotNone(resolve(path))
        self.assertContains(response, '<a href="/app/reviews/">')
        self.assertNotContains(response, '<a href="/app/approval-inbox/')

    def test_synthetic_workspace_shows_role_aware_dismissible_judge_walkthrough(self):
        self.workspace.is_demo = True
        self.workspace.save(update_fields=["is_demo"])
        creator = self.create_member("walkthrough_creator", WorkspaceMembership.Role.CREATOR)
        self.client.force_login(creator)
        response = self.client.get(reverse("app:home"))
        self.assertContains(response, "A short product walkthrough")
        self.assertContains(response, "Create the sample")
        self.assertContains(response, reverse("app:content_reviews:create"))
        self.assertContains(response, "separate Reviewer demo account")
        self.assertContains(response, "data-dismiss-demo-walkthrough")
        self.assertContains(response, "data-demo-guide-key")
        self.assertNotContains(response, "password=")

        reviewer = self.create_member("walkthrough_reviewer", WorkspaceMembership.Role.REVIEWER)
        self.client.force_login(reviewer)
        reviewer_page = self.client.get(reverse("app:home"))
        self.assertContains(reviewer_page, "Make a human decision")
        self.assertContains(reviewer_page, reverse("app:approvals:inbox"))
        self.assertNotContains(reviewer_page, reverse("app:content_reviews:create"))

    def test_non_demo_workspace_does_not_show_the_judge_walkthrough(self):
        creator = self.create_member("ordinary_creator", WorkspaceMembership.Role.CREATOR)
        self.client.force_login(creator)
        response = self.client.get(reverse("app:home"))
        self.assertNotContains(response, "A short product walkthrough")
        self.assertNotContains(response, "data-demo-walkthrough")

    def test_admin_can_view_only_current_workspace_member_records(self):
        admin = self.create_member("workspace_admin", WorkspaceMembership.Role.ADMIN)
        creator = self.create_member("current_creator", WorkspaceMembership.Role.CREATOR)
        self.create_member("current_inactive", WorkspaceMembership.Role.REVIEWER, active=False)
        other_workspace = Workspace.objects.create(name="Separate", slug="separate-workspace")
        outsider = self.create_member(
            "other_workspace_person", WorkspaceMembership.Role.REVIEWER, workspace=other_workspace
        )

        self.client.force_login(admin)
        response = self.client.get(reverse("app:members"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, creator.username)
        self.assertContains(response, "Inactive")
        self.assertContains(response, ">3</span> members")
        self.assertNotContains(response, outsider.username)
        self.assertNotContains(response, outsider.email)

    def test_non_admin_members_access_is_denied_for_each_other_role(self):
        for index, role in enumerate(
            [WorkspaceMembership.Role.CREATOR, WorkspaceMembership.Role.BRAND_MANAGER, WorkspaceMembership.Role.REVIEWER]
        ):
            with self.subTest(role=role):
                user = self.create_member(f"denied_user_{index}", role)
                self.client.force_login(user)
                self.assertEqual(self.client.get(reverse("app:members")).status_code, 403)
                self.client.logout()

    def test_anonymous_members_access_redirects_to_login(self):
        response = self.client.get(reverse("app:members"))
        self.assertRedirects(
            response,
            f"{reverse('accounts:login')}?next={reverse('app:members')}",
            fetch_redirect_response=False,
        )

    def test_members_page_is_read_only(self):
        admin = self.create_member("read_only_admin", WorkspaceMembership.Role.ADMIN)
        self.client.force_login(admin)
        self.assertEqual(self.client.post(reverse("app:members"), {"role": "CREATOR"}).status_code, 405)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class DemoSeedCommandTests(TestCase):
    def setUp(self):
        self.demo_password = secrets.token_urlsafe(24)
        self.env_keys = [
            "BRANDGUARD_DEMO_PASSWORD",
            "BRANDGUARD_DEMO_CREATOR_PASSWORD",
            "BRANDGUARD_DEMO_BRAND_MANAGER_PASSWORD",
            "BRANDGUARD_DEMO_REVIEWER_PASSWORD",
            "BRANDGUARD_DEMO_ADMIN_PASSWORD",
        ]

    @override_settings(DEBUG=True)
    def test_seed_creates_four_roles_and_is_idempotent(self):
        empty_env = {key: "" for key in self.env_keys}
        first_output, second_output = __import__("io").StringIO(), __import__("io").StringIO()
        with mock.patch.dict("os.environ", empty_env):
            call_command("seed_demo", stdout=first_output)
            first = first_output.getvalue()
            call_command("seed_demo", stdout=second_output)
        self.assertEqual(Workspace.objects.filter(slug="lumadesk-demo").count(), 1)
        self.assertEqual(WorkspaceMembership.objects.filter(workspace__slug="lumadesk-demo").count(), 4)
        users = get_user_model().objects.filter(username__startswith="lumadesk_")
        self.assertEqual(users.count(), 4)
        for username, role in [
            ("lumadesk_creator", WorkspaceMembership.Role.CREATOR),
            ("lumadesk_brand_manager", WorkspaceMembership.Role.BRAND_MANAGER),
            ("lumadesk_reviewer", WorkspaceMembership.Role.REVIEWER),
            ("lumadesk_admin", WorkspaceMembership.Role.ADMIN),
        ]:
            membership = WorkspaceMembership.objects.get(user__username=username)
            self.assertEqual(membership.role, role)
            self.assertTrue(membership.is_demo)
            self.assertTrue(membership.is_active)
            self.assertFalse(membership.user.is_staff)
        self.assertIn("One-time generated password", first)
        self.assertNotIn("One-time generated password", second_output.getvalue())
        self.assertIn("existing password left unchanged", second_output.getvalue())

    @override_settings(DEBUG=True)
    def test_seed_uses_env_password_without_echoing_it(self):
        values = {key: self.demo_password for key in self.env_keys}
        output = __import__("io").StringIO()
        with mock.patch.dict("os.environ", values):
            call_command("seed_demo", stdout=output)
        self.assertNotIn(self.demo_password, output.getvalue())
        for username in ["lumadesk_creator", "lumadesk_brand_manager", "lumadesk_reviewer", "lumadesk_admin"]:
            user = get_user_model().objects.get(username=username)
            self.assertTrue(user.check_password(self.demo_password))

    @override_settings(DEBUG=False)
    def test_seed_refuses_non_debug_configuration(self):
        with self.assertRaises(CommandError):
            call_command("seed_demo", stdout=__import__("io").StringIO())
        self.assertFalse(Workspace.objects.filter(slug="lumadesk-demo").exists())

    @override_settings(DEBUG=True)
    def test_seed_does_not_overwrite_unrelated_username_collision(self):
        password = secrets.token_urlsafe(24)
        unrelated = get_user_model().objects.create_user(
            username="lumadesk_creator", password=password
        )
        with self.assertRaises(CommandError):
            call_command("seed_demo", stdout=__import__("io").StringIO())
        unrelated.refresh_from_db()
        self.assertTrue(unrelated.check_password(password))
        self.assertFalse(Workspace.objects.filter(slug="lumadesk-demo").exists())
