from django.conf import settings
from django.db import models

from tenant.common.models.tenant_base_model import TenantBaseModel


class AvailabilityProfile(TenantBaseModel):
    """
    A user's working-hours container. Weekly working blocks live in the related
    AvailabilityRule rows. Tier 2 (audited).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="availability_profiles",
        help_text="The user whose working hours this profile describes.",
    )
    timezone = models.ForeignKey(
        "onboard.Timezone",
        on_delete=models.PROTECT,
        related_name="availability_profiles",
        help_text="IANA timezone the rule wall-clock times are expressed in (DST-aware).",
    )
    name = models.CharField(
        max_length=100,
        default="Default",
        help_text="Label for this profile.",
    )

    class Meta:
        db_table = "core_availability_profiles"
        verbose_name = "Availability Profile"
        verbose_name_plural = "Availability Profiles"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "tenant"],
                condition=models.Q(is_deleted=False),
                name="unique_active_availability_profile_user",
            ),
        ]

    def soft_delete(self, user=None):
        """Soft-delete, and drop the availability this profile had materialized.

        AvailabilityWindow rows are derived data keyed on the profile. Leaving
        them behind keeps the host bookable from a deleted profile until the
        daily horizon task next runs — up to 24 hours of slots that should not
        exist.

        This lives on the model rather than in the viewset so that every path
        gets it: the REST delete, `bulk_delete` (which calls `soft_delete`
        directly and would otherwise skip the purge), and a shell session alike.
        """
        super().soft_delete(user=user)
        from core.services import availability_materializer as materializer

        materializer.purge_profile(self)

    def __str__(self):
        return f"{self.name} ({self.user_id})"
