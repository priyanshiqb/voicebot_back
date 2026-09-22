from django.db import models
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Lower, Trim
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

    @classmethod
    def name_in_use(cls, name, tenant=None, exclude_pk=None):
        """Whether a live voice already has `name`, ignoring case and surrounding spaces.

        The name is the payload's `name` key, not a column, so it is read out of the
        JSON. Shared by create validation and the availability check so the two cannot
        disagree. Deleted voices release their name.
        """
        queryset = (
            cls.objects.filter(is_deleted=False)
            .annotate(_name=Lower(Trim(KeyTextTransform("name", "data"))))
            .filter(_name=name.strip().lower())
        )
        if tenant is not None:
            queryset = queryset.filter(tenant=tenant)
        if exclude_pk is not None:
            queryset = queryset.exclude(pk=exclude_pk)
        return queryset.exists()

    def __str__(self):
        # Fallback to string ID if name is not inside the data
        return self.data.get("name", str(self.id))
