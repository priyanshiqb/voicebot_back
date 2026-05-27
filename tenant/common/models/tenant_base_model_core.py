from django.db import models
from utils.models.base_model_core import BaseModelCore
from tenant.models.Tenant import Tenant


class TenantBaseModelCore(BaseModelCore):
    """
    Core Tenant-scoped Base Model.
    Inherits from BaseModelCore and adds a non-nullable Foreign Key back to the Tenant model,
    automatically handling namespace-safe related queries via unique related_name mappings.
    """
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_tenant",
        help_text="Tenant context partition key for row-level security isolation."
    )

    class Meta(BaseModelCore.Meta):
        abstract = True
