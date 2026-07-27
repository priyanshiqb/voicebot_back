from django.conf import settings
from django.db import models

from tenant.common.models.tenant_base_model_core import TenantBaseModelCore


class UserEventSkill(TenantBaseModelCore):
    """
    Junction defining which users (hosts) can host which EventTemplates.
    High-volume routing table, so Tier 1 Core (no user-audit FKs).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="event_skills",
        help_text="The user (host) able to host the linked event type.",
    )
    event_template = models.ForeignKey(
        "core.EventTemplate",
        on_delete=models.CASCADE,
        related_name="skills",
        help_text="The event type this user is eligible to host.",
    )
    weight = models.PositiveSmallIntegerField(
        default=1,
        help_text="Relative bias for round-robin selection (higher = more often).",
    )

    class Meta:
        db_table = "core_user_event_skills"
        verbose_name = "User Event Skill"
        verbose_name_plural = "User Event Skills"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "event_template", "tenant"],
                condition=models.Q(is_deleted=False),
                name="unique_active_user_event_skill",
            ),
        ]

    def __str__(self):
        return f"{self.user_id} :: {self.event_template_id}"
