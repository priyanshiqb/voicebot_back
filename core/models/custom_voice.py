from django.db import models
from tenant.common.models.tenant_base_model import TenantBaseModel

class CustomVoice(TenantBaseModel):
    """
    Model representing custom trained voices for a tenant.
    Data is stored in a JSONField as requested.
    """
    data = models.JSONField(
        default=dict,
        help_text="Stores the entire custom voice JSON payload provided by the frontend."
    )

    class Meta:
        db_table = "core_custom_voices"
        verbose_name = "Custom Voice"
        verbose_name_plural = "Custom Voices"
        ordering = ["-created_at"]

    def __str__(self):
        # Fallback to string ID if name is not inside the data
        return self.data.get("name", str(self.id))
