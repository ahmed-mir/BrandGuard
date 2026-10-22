import re
import secrets

from django.contrib.auth import get_user_model
from django.contrib.auth import authenticate
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from workspaces.models import Workspace, WorkspaceMembership


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AuthenticationFlowTests(TestCase):
    def setUp(self):
        self.password = secrets.token_urlsafe(24)
        self.user = get_user_model().objects.create_user(
            username="reviewer_one",
            email="reviewer.one@example.test",
            password=self.password,
            first_name="Riley",
            last_name="Review",
        )
        self.workspace = Workspace.objects.create(name="LumaDesk", slug="lumadesk-test")
        self.membership = WorkspaceMembership.objects.create(
            user=self.user,
            workspace=self.workspace,
            role=WorkspaceMembership.Role.REVIEWER,
        )

    def test_login_by_username_uses_django_session(self):
        response = self.client.post(
            reverse("accounts:login"),
            {"username": self.user.username, "password": self.password},
        )
        self.assertRedirects(response, reverse("app:home"), fetch_redirect_response=False)
        self.assertEqual(str(self.user.pk), self.client.session["_auth_user_id"])

    def test_login_by_case_insensitive_email(self):
        response = self.client.post(
            reverse("accounts:login"),
            {"username": self.user.email.upper(), "password": self.password},
        )
        self.assertRedirects(response, reverse("app:home"), fetch_redirect_response=False)
        self.assertEqual(str(self.user.pk), self.client.session["_auth_user_id"])

    def test_invalid_credentials_are_rejected_with_generic_feedback(self):
        response = self.client.post(
            reverse("accounts:login"),
            {"username": self.user.username, "password": secrets.token_urlsafe(24)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Those details do not match an active account")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_inactive_account_cannot_authenticate(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        response = self.client.post(
            reverse("accounts:login"),
            {"username": self.user.username, "password": self.password},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_external_post_login_redirect_is_rejected(self):
        response = self.client.post(
            reverse("accounts:login"),
            {
                "username": self.user.username,
                "password": self.password,
                "next": "https://outside.example/",
            },
        )
        self.assertRedirects(response, reverse("app:home"), fetch_redirect_response=False)

    def test_anonymous_workspace_request_redirects_to_login(self):
        response = self.client.get(reverse("app:home"))
        self.assertRedirects(
            response,
            f"{reverse('accounts:login')}?next={reverse('app:home')}",
            fetch_redirect_response=False,
        )

    def test_logout_requires_post_and_clears_session(self):
        self.client.force_login(self.user)
        get_response = self.client.get(reverse("accounts:logout"))
        self.assertEqual(get_response.status_code, 405)
        self.assertIn("_auth_user_id", self.client.session)

        response = self.client.post(reverse("accounts:logout"))
        self.assertRedirects(response, reverse("landing"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_csrf_is_required_for_login_and_logout_posts(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.get(reverse("accounts:login"))
        denied = csrf_client.post(
            reverse("accounts:login"),
            {"username": self.user.username, "password": self.password},
        )
        self.assertEqual(denied.status_code, 403)

        form_page = csrf_client.get(reverse("accounts:login"))
        token_match = re.search(
            r'name="csrfmiddlewaretoken" value="([^"]+)"', form_page.content.decode()
        )
        self.assertIsNotNone(token_match)
        token = token_match.group(1)
        signed_in = csrf_client.post(
            reverse("accounts:login"),
            {"username": self.user.username, "password": self.password,
                "csrfmiddlewaretoken": token},
        )
        self.assertEqual(signed_in.status_code, 302)
        app_page = csrf_client.get(reverse("app:home"))
        self.assertContains(app_page, "csrfmiddlewaretoken")
        token_match = re.search(
            r'name="csrfmiddlewaretoken" value="([^"]+)"', app_page.content.decode()
        )
        self.assertIsNotNone(token_match)
        logged_out = csrf_client.post(
            reverse("accounts:logout"),
            {"csrfmiddlewaretoken": token_match.group(1)},
        )
        self.assertEqual(logged_out.status_code, 302)
        self.assertNotIn("_auth_user_id", csrf_client.session)

    def test_no_public_registration_route_or_role_form_field_exists(self):
        response = self.client.get(reverse("accounts:login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No public registration")
        self.assertNotContains(response, 'name="role"')
        self.assertNotContains(response, 'name="workspace_id"')


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ProfileSettingsTests(TestCase):
    def setUp(self):
        self.password = secrets.token_urlsafe(24)
        self.user = get_user_model().objects.create_user(
            username="profile_person",
            email="profile.person@example.test",
            password=self.password,
            first_name="Taylor",
            last_name="Writer",
        )
        self.workspace = Workspace.objects.create(name="LumaDesk", slug="lumadesk-profile")
        self.membership = WorkspaceMembership.objects.create(
            user=self.user,
            workspace=self.workspace,
            role=WorkspaceMembership.Role.CREATOR,
        )
        self.client.force_login(self.user)

    def test_profile_page_shows_read_only_identity_and_workspace_details(self):
        response = self.client.get(reverse("app:profile"))
        self.assertEqual(response.status_code, 200)
        for visible in [
            "profile_person",
            "profile.person@example.test",
            "LumaDesk",
            "Creator",
            "First name",
            "Last name",
            "Current password",
            "New password",
        ]:
            self.assertContains(response, visible)
        self.assertNotContains(response, self.password)
        self.assertNotContains(response, 'name="email"')
        self.assertNotContains(response, 'name="username"')
        self.assertNotContains(response, 'name="role"')

    def test_anonymous_user_cannot_access_profile_settings(self):
        self.client.logout()
        response = self.client.get(reverse("app:profile"))
        self.assertRedirects(
            response,
            f"{reverse('accounts:login')}?next={reverse('app:profile')}",
            fetch_redirect_response=False,
        )

    def test_valid_name_update_saves_only_profile_fields(self):
        response = self.client.post(
            reverse("app:profile"),
            {
                "form_action": "profile",
                "first_name": "Morgan",
                "last_name": "Editor",
                "username": "attacker_changed_name",
                "email": "attacker@example.test",
                "role": WorkspaceMembership.Role.ADMIN,
            },
        )
        self.assertRedirects(response, reverse("app:profile"), fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.membership.refresh_from_db()
        self.assertEqual((self.user.first_name, self.user.last_name), ("Morgan", "Editor"))
        self.assertEqual(self.user.username, "profile_person")
        self.assertEqual(self.user.email, "profile.person@example.test")
        self.assertEqual(self.membership.role, WorkspaceMembership.Role.CREATOR)
        self.assertContains(self.client.get(reverse("app:profile")), "account details have been updated")

    def test_invalid_name_update_shows_feedback_without_saving(self):
        response = self.client.post(
            reverse("app:profile"),
            {"form_action": "profile", "first_name": "x" * 151, "last_name": "Valid"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["profile_form"].errors)
        self.assertContains(response, "aria-invalid=\"true\"")
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Taylor")

    def test_password_change_uses_django_validation_and_preserves_session(self):
        new_password = secrets.token_urlsafe(28)
        response = self.client.post(
            reverse("app:profile"),
            {
                "form_action": "password",
                "old_password": self.password,
                "new_password1": new_password,
                "new_password2": new_password,
            },
        )
        self.assertRedirects(response, reverse("app:profile"), fetch_redirect_response=False)
        self.assertEqual(self.client.session.get("_auth_user_id"), str(self.user.pk))
        self.assertIsNotNone(authenticate(username="profile_person", password=new_password))
        self.assertIsNone(authenticate(username="profile_person", password=self.password))
        profile_response = self.client.get(reverse("app:profile"))
        self.assertContains(profile_response, "Your password has been changed")
        self.assertNotContains(profile_response, new_password)
        self.assertEqual(self.client.get(reverse("app:home")).status_code, 200)

    def test_invalid_password_change_keeps_existing_password_and_session(self):
        response = self.client.post(
            reverse("app:profile"),
            {
                "form_action": "password",
                "old_password": "incorrect-current-password",
                "new_password1": "short",
                "new_password2": "does-not-match",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["password_form"].errors)
        self.assertEqual(self.client.session.get("_auth_user_id"), str(self.user.pk))
        self.assertIsNotNone(authenticate(username="profile_person", password=self.password))

    def test_profile_and_password_mutations_require_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        csrf_client.get(reverse("app:profile"))
        profile_denied = csrf_client.post(
            reverse("app:profile"),
            {"form_action": "profile", "first_name": "Changed", "last_name": "Name"},
        )
        password_denied = csrf_client.post(
            reverse("app:profile"),
            {
                "form_action": "password",
                "old_password": self.password,
                "new_password1": secrets.token_urlsafe(24),
                "new_password2": secrets.token_urlsafe(24),
            },
        )
        self.assertEqual(profile_denied.status_code, 403)
        self.assertEqual(password_denied.status_code, 403)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Taylor")
        self.assertTrue(self.user.check_password(self.password))
