import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from content_reviews.models import ContentSubmission, ReviewRun
from content_reviews.services import create_submission, execute_review
from workspaces.models import Workspace, WorkspaceMembership


SCENARIOS = (
    (
        "Scenario A — Prohibited terminology",
        "Our tool is called Luma Desk.",
        "INSTAGRAM",
    ),
    (
        "Scenario B — Correct terminology",
        "LumaDesk brings project work together.",
        "INSTAGRAM",
    ),
    (
        "Scenario C — Applicable disclaimer missing",
        "Up to 20% faster task completion in our internal pilot.",
        "EMAIL",
    ),
    (
        "Scenario D — Supported approved fact",
        "Up to 20% faster task completion in our internal pilot; results vary.",
        "WEBSITE",
    ),
    (
        "Scenario E — Non-comparable numeric unit",
        "Task completion speed was 14 tasks per shift faster during the pilot.",
        "WEBSITE",
    ),
    (
        "Scenario F — No applicable violations",
        "LumaDesk helps teams plan clearly. Results vary.",
        "LINKEDIN",
    ),
)


class Command(BaseCommand):
    help = "Create and genuinely review synthetic LumaDesk content scenarios for local development."

    def handle(self, *args, **options):
        if not settings.DEBUG or connection.vendor != "sqlite":
            raise CommandError("seed_review_scenarios is restricted to DEBUG mode with the local SQLite database.")

        try:
            workspace = Workspace.objects.get(slug="lumadesk-demo", is_demo=True, is_active=True)
            membership = WorkspaceMembership.objects.select_related("user").get(
                workspace=workspace,
                user__username="lumadesk_creator",
                role=WorkspaceMembership.Role.CREATOR,
                is_active=True,
                is_demo=True,
            )
        except (Workspace.DoesNotExist, WorkspaceMembership.DoesNotExist):
            raise CommandError("Run `python manage.py seed_demo` first to create the LumaDesk demo workspace and creator account.")

        created = 0
        reviewed = 0
        with transaction.atomic():
            for title, text, channel in SCENARIOS:
                submission = ContentSubmission.objects.filter(
                    workspace=workspace,
                    creator=membership.user,
                    title=title,
                    is_demo=True,
                ).first()
                if submission is None:
                    submission, _ = create_submission(
                        workspace=workspace,
                        creator=membership.user,
                        title=title,
                        channel=channel,
                        audience="Synthetic demonstration audience",
                        campaign_purpose="Exercise the deterministic text review engine",
                        content_text=text,
                        is_demo=True,
                    )
                    created += 1

                version = submission.current_version
                existing_run = ReviewRun.objects.filter(content_version=version, requested_by=membership.user).first()
                if existing_run:
                    continue
                token = uuid.uuid5(uuid.NAMESPACE_URL, f"brandguard-demo-review:{submission.pk}:{version.pk}")
                execute_review(
                    submission_id=submission.pk,
                    version_number=version.version_number,
                    workspace=workspace,
                    actor=membership.user,
                    idempotency_key=token,
                )
                reviewed += 1

        self.stdout.write(self.style.SUCCESS(
            f"Review scenarios ready for {workspace.name}: {created} new submissions, {reviewed} new real review runs; existing scenarios were preserved."
        ))
