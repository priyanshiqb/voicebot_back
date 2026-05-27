from django.db import models
from utils.models.base_model import BaseModel
from tenant.models.Tenant import Tenant


class TenantBaseModel(BaseModel):
    """
    Audited Tenant-scoped Base Model.
    Extends BaseModel directly and adds the tenant Foreign Key for row-level tenant isolation,
    reusing all audit fields and soft-delete methods to avoid redundancy.
    """
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_tenant",
        help_text="Tenant context partition key for row-level security isolation."
    )

    class Meta(BaseModel.Meta):
        abstract = True
