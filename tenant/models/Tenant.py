from django.db import models
from utils.models.base_model_core import BaseModelCore


class Tenant(BaseModelCore):
    """
    Core Tenant model representing customer organizations/tenants.
    Inherits from BaseModelCore for UUID primary keys, soft deletion, and timestamps.
    """
    name = models.CharField(max_length=255)
    subdomain = models.CharField(max_length=100, unique=True, db_index=True)

    class Meta(BaseModelCore.Meta):
        verbose_name = "Tenant"
        verbose_name_plural = "Tenants"

    def __str__(self):
        return self.name
