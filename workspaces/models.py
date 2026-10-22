from django.conf import settings
from django.db import models


class Workspace(models.Model):
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True)
    is_active = models.BooleanField(default=True)
    is_demo = models.BooleanField(default=False)
    policy_revision = models.PositiveBigIntegerField(default=0)
    policy_snapshot_hash = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name


class WorkspaceMembership(models.Model):
    class Role(models.TextChoices):
        CREATOR = "CREATOR", "Creator"
        BRAND_MANAGER = "BRAND_MANAGER", "Brand manager"
        REVIEWER = "REVIEWER", "Reviewer"
        ADMIN = "ADMIN", "Workspace admin"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="workspace_memberships",
    )
    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    role = models.CharField(max_length=20, choices=Role.choices)
    is_active = models.BooleanField(default=True)
    is_demo = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["workspace__name", "user__username"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "workspace"],
                name="unique_workspace_user_membership",
            )
        ]
        indexes = [
            models.Index(
                fields=["user", "is_active"],
                name="membership_user_active_idx",
            ),
        ]

    def __str__(self):
        return f"{self.user} — {self.workspace} ({self.get_role_display()})"
