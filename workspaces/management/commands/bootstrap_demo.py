"""Explicit, append-safe provisioning of the synthetic judge demo on PostgreSQL."""
import os

from django.conf import settings
from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from brand_sources.demo import seed_lumadesk_source_pack
from workspaces.models import Workspace, WorkspaceMembership


DEMO_WORKSPACE = {"name": "LumaDesk", "slug": "lumadesk-demo"}
DEMO_ACCOUNTS = (
    ("lumadesk_creator", "creator@lumadesk.test", WorkspaceMembership.Role.CREATOR, "CREATOR"),
    ("lumadesk_brand_manager", "brand.manager@lumadesk.test", WorkspaceMembership.Role.BRAND_MANAGER, "BRAND_MANAGER"),
    ("lumadesk_reviewer", "reviewer@lumadesk.test", WorkspaceMembership.Role.REVIEWER, "REVIEWER"),
)


class Command(BaseCommand):
    help = "Provision the synthetic LumaDesk demo on PostgreSQL after explicit operator authorization."

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm-production-demo-bootstrap",
            action="store_true",
            help="Confirm that the operator intends to create demo records in the configured production database.",
        )

    def handle(self, *args, **options):
        if not getattr(settings, "IS_PRODUCTION", False) or settings.DEBUG:
            raise CommandError("bootstrap_demo is only for an explicitly configured production environment.")
        if not options.get("confirm_production_demo_bootstrap"):
            raise CommandError("Pass --confirm-production-demo-bootstrap after verifying the target database.")
        if os.environ.get("BRANDGUARD_ALLOW_PRODUCTION_DEMO_BOOTSTRAP") != "YES":
            raise CommandError("Set BRANDGUARD_ALLOW_PRODUCTION_DEMO_BOOTSTRAP=YES as a second explicit opt-in.")
        if connection.vendor != "postgresql":
            raise CommandError("bootstrap_demo requires a PostgreSQL target database; no data was changed.")

        user_model = get_user_model()
        passwords = self._preflight_accounts(user_model)

        with transaction.atomic():
            workspace, created = Workspace.objects.get_or_create(
                slug=DEMO_WORKSPACE["slug"],
                defaults={"name": DEMO_WORKSPACE["name"], "is_demo": True},
            )
            if not workspace.is_demo or workspace.name != DEMO_WORKSPACE["name"] or not workspace.is_active:
                raise CommandError("The LumaDesk demo workspace identity is occupied or inactive; no data was changed.")

            users = {}
            for username, email, role, key in DEMO_ACCOUNTS:
                user = user_model._default_manager.filter(username=username).first()
                if user is None:
                    user = user_model._default_manager.create_user(
                        username=username,
                        email=email,
                        password=passwords[username],
                        is_active=True,
                        is_staff=False,
                        is_superuser=False,
                    )
                membership, membership_created = WorkspaceMembership.objects.get_or_create(
                    user=user,
                    workspace=workspace,
                    defaults={"role": role, "is_active": True, "is_demo": True},
                )
                if (
                    not membership.is_demo
                    or membership.role != role
                    or not membership.is_active
                    or user.is_staff
                    or user.is_superuser
                    or not user.is_active
                ):
                    raise CommandError("An existing demo identity has conflicting or privileged account data; no data was changed.")
                users[username] = user

            sources = seed_lumadesk_source_pack(
                workspace=workspace,
                owner=users["lumadesk_brand_manager"],
            )

        self.stdout.write(self.style.SUCCESS(
            f"Synthetic LumaDesk demo is ready ({len(sources)} source records; workspace {'created' if created else 'already existed'})."
        ))
        self.stdout.write("Demo accounts: lumadesk_creator, lumadesk_brand_manager, lumadesk_reviewer.")
        self.stdout.write("Credentials are taken from the role-specific environment variables and are never displayed or reset.")

    def _preflight_accounts(self, user_model):
        workspace = Workspace.objects.filter(slug=DEMO_WORKSPACE["slug"]).first()
        passwords = {}
        supplied_passwords = []
        for username, email, role, key in DEMO_ACCOUNTS:
            user = user_model._default_manager.filter(username=username).first()
            if user:
                membership = (
                    WorkspaceMembership.objects.filter(user=user, workspace=workspace).first()
                    if workspace else None
                )
                if (
                    membership is None
                    or not membership.is_demo
                    or membership.role != role
                    or not membership.is_active
                    or not user.is_active
                    or user.is_staff
                    or user.is_superuser
                ):
                    raise CommandError("A demo username already belongs to non-demo or privileged account data; no data was changed.")
                passwords[username] = None
                continue
            if user_model._default_manager.filter(email__iexact=email).exists():
                raise CommandError("A demo email is already assigned to another account; no data was changed.")
            password = os.environ.get(f"BRANDGUARD_DEMO_{key}_PASSWORD", "")
            if len(password) < 20:
                raise CommandError(f"Set a unique, strong BRANDGUARD_DEMO_{key}_PASSWORD for each new demo account.")
            try:
                password_validation.validate_password(password, user=user_model(username=username, email=email))
            except ValidationError as exc:
                raise CommandError(f"BRANDGUARD_DEMO_{key}_PASSWORD does not meet the configured password policy.") from exc
            supplied_passwords.append(password)
            passwords[username] = password

        if len(supplied_passwords) != len(set(supplied_passwords)):
            raise CommandError("Use a different password for each new demo account; no data was changed.")
        return passwords
