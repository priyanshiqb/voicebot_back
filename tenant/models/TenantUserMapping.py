from django.db import models
from django.conf import settings
from utils.models.base_model import BaseModel
from tenant.models.Tenant import Tenant


class TenantUserMapping(BaseModel):
    """
    Tenant User Mapping Model.
    Enforces a strict 1-to-1 association where each user account is mapped to exactly one tenant organization.
    Inherits from global BaseModel for user auditing and soft-deletion capability.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tenant_mapping",
        help_text="Strict 1:1 user profile assignment constraint."
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="user_mappings",
        help_text="Mapped organization/tenant ownership."
    )

    class Meta(BaseModel.Meta):
        verbose_name = "Tenant User Mapping"
        verbose_name_plural = "Tenant User Mappings"

    def __str__(self):
        return f"{self.user.username} -> {self.tenant.name}"
