from core.models.agent_template import AgentTemplate
from .base_resource import TenantModelResource


class AgentTemplateResource(TenantModelResource):
    # Exported columns, in this order. `id` and `created_at` are deliberately not
    # among them — see TenantModelResource.EXPORT_FIELDS for why this cannot be
    # expressed through Meta.fields alone.
    EXPORT_FIELDS = (
        "name",
        "short_description",
        "industry",
        "strict_facts",
        "can_user_edit_flow",
        "is_active",
    )

    class Meta:
        model = AgentTemplate
        # `id` stays importable so a file carrying one updates the row it names
        # instead of duplicating it.
        fields = (
            "id",
            "name",
            "short_description",
            "industry",
            "strict_facts",
            "can_user_edit_flow",
            "is_active",
        )
