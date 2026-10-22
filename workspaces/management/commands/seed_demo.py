import os
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from workspaces.models import Workspace, WorkspaceMembership


DEMO_WORKSPACE = {"name": "LumaDesk", "slug": "lumadesk-demo"}
DEMO_ACCOUNTS = (
    ("lumadesk_creator", "creator@lumadesk.test", WorkspaceMembership.Role.CREATOR, "CREATOR"),
    ("lumadesk_brand_manager", "brand.manager@lumadesk.test", WorkspaceMembership.Role.BRAND_MANAGER, "BRAND_MANAGER"),
    ("lumadesk_reviewer", "reviewer@lumadesk.test", WorkspaceMembership.Role.REVIEWER, "REVIEWER"),
    ("lumadesk_admin", "admin@lumadesk.test", WorkspaceMembership.Role.ADMIN, "ADMIN"),
)


class Command(BaseCommand):
    help = "Create safe, repeatable local LumaDesk demo accounts and memberships."

    def handle(self, *args, **options):
        if not settings.DEBUG or connection.vendor != "sqlite":
            raise CommandError(
                "seed_demo is restricted to DEBUG mode with the local SQLite database."
            )

        user_model = get_user_model()
        planned_passwords = {}
        supplied_by_env = {}
        for username, email, role, key in DEMO_ACCOUNTS:
            env_key = f"BRANDGUARD_DEMO_{key}_PASSWORD"
            password = os.environ.get(env_key) or os.environ.get("BRANDGUARD_DEMO_PASSWORD")
            is_generated = not password
            if is_generated:
                password = secrets.token_urlsafe(24)
            if len(password) < 16:
                raise CommandError(f"{env_key} must contain at least 16 characters.")
            candidate = user_model(username=username, email=email)
            try:
                password_validation.validate_password(password, user=candidate)
            except ValidationError as exc:
                raise CommandError(f"The password configured by {env_key} is too weak: {exc}") from exc
            planned_passwords[username] = password if is_generated else None
            supplied_by_env[username] = (
                env_key if os.environ.get(env_key) else
                "BRANDGUARD_DEMO_PASSWORD" if os.environ.get("BRANDGUARD_DEMO_PASSWORD") else None
            )

        created = []
        reused = []
        with transaction.atomic():
            workspace, _ = Workspace.objects.get_or_create(
                slug=DEMO_WORKSPACE["slug"],
                defaults={"name": DEMO_WORKSPACE["name"], "is_demo": True},
            )
            if not workspace.is_demo or workspace.name != DEMO_WORKSPACE["name"]:
                raise CommandError(
                    "The lumadesk-demo slug belongs to a non-demo or renamed workspace; no data was changed."
                )
            if not workspace.is_active:
                workspace.is_active = True
                workspace.save(update_fields=["is_active"])

            for username, email, role, key in DEMO_ACCOUNTS:
                user = user_model._default_manager.filter(username=username).first()
                if user is None:
                    if user_model._default_manager.filter(email__iexact=email).exists():
                        raise CommandError(
                            f"The demo email {email} is already assigned to another account; no data was changed."
                        )
                    user = user_model._default_manager.create_user(
                        username=username,
                        email=email,
                        password=(
                            planned_passwords[username]
                            or os.environ.get(f"BRANDGUARD_DEMO_{key}_PASSWORD")
                            or os.environ.get("BRANDGUARD_DEMO_PASSWORD")
                        ),
                        is_active=True,
                        is_staff=False,
                        is_superuser=False,
                    )
                    created.append(username)
                else:
                    membership = WorkspaceMembership.objects.filter(
                        user=user, workspace=workspace
                    ).first()
                    if membership is None or not membership.is_demo:
                        raise CommandError(
                            f"Username {username} already exists outside this demo seed; no data was changed."
                        )
                    if not user.is_active:
                        raise CommandError(
                            f"Seeded account {username} is inactive; reactivate it deliberately before reseeding."
                        )
                    reused.append(username)

                membership, membership_created = WorkspaceMembership.objects.get_or_create(
                    user=user,
                    workspace=workspace,
                    defaults={"role": role, "is_active": True, "is_demo": True},
                )
                if membership_created:
                    continue
                if not membership.is_demo:
                    raise CommandError(
                        f"Membership for {username} is not owned by the demo seed; no data was changed."
                    )
                updates = []
                if membership.role != role:
                    membership.role = role
                    updates.append("role")
                if not membership.is_active:
                    membership.is_active = True
                    updates.append("is_active")
                if updates:
                    membership.save(update_fields=updates)

        self.stdout.write(self.style.SUCCESS(
            f"LumaDesk demo workspace ready: {workspace.name} ({workspace.slug})"
        ))
        for username, email, role, key in DEMO_ACCOUNTS:
            role_label = dict(WorkspaceMembership.Role.choices)[role]
            if username in created:
                self.stdout.write(f"  {username} / {email} | {role_label}")
                if supplied_by_env[username]:
                    self.stdout.write(
                        f"    Use the password from {supplied_by_env[username]} (value not displayed)."
                    )
                else:
                    self.stdout.write(
                        f"    One-time generated password: {planned_passwords[username]}"
                    )
            else:
                self.stdout.write(
                    f"  {username} / {email} | {role_label}; existing password left unchanged."
                )
        if any(planned_passwords[username] for username in created):
            self.stdout.write(
                self.style.WARNING(
                    "Capture generated passwords securely now; they are not stored or shown again."
                )
            )
