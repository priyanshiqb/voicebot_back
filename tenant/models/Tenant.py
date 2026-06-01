from django.db import models
from utils.models.base_model_core import BaseModelCore


import uuid


class Tenant(BaseModelCore):
    """
    Core Tenant model representing customer organizations/tenants.
    Inherits from BaseModelCore for UUID primary keys, soft deletion, and timestamps.
    """
    name = models.CharField(max_length=255)
    subdomain = models.CharField(max_length=100, unique=True, db_index=True)

    referred_by_code = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text="The referral code used by this tenant during onboarding."
    )
    tenant_referral_code = models.CharField(
        max_length=50,
        unique=True,
        blank=True,
        null=True,
        help_text="Unique referral code generated for this tenant to refer others."
    )

    class Meta(BaseModelCore.Meta):
        verbose_name = "Tenant"
        verbose_name_plural = "Tenants"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.tenant_referral_code:
            # Inline import to prevent circular import errors
            from tenant.models.Tenant import Tenant as TenantModel
            while True:
                code = f"REF-{uuid.uuid4().hex[:8].upper()}"
                if not TenantModel.objects.filter(tenant_referral_code=code).exists():
                    self.tenant_referral_code = code
                    break
        super().save(*args, **kwargs)
