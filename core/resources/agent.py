from core.models.agent import Agent
from .base_resource import TenantModelResource


class AgentResource(TenantModelResource):
    # Exported columns, in this order. `id` and `created_at` are deliberately not
    # among them — see TenantModelResource.EXPORT_FIELDS for why this cannot be
    # expressed through Meta.fields alone.
    EXPORT_FIELDS = (
        "bot_name",
        "persona_name",
        "languages",
        "tone",
        "voice_name",
        "is_active",
    )

    class Meta:
        model = Agent
        # `id` stays importable so a file carrying one updates the row it names
        # instead of duplicating it.
        fields = (
            "id",
            "bot_name",
            "persona_name",
            "languages",
            "tone",
            "voice_name",
            "is_active",
        )
