from core.models.probing_question import ProbingQuestion
from .base_resource import TenantModelResource


class ProbingQuestionResource(TenantModelResource):
    # Exported columns, in this order. `id` and `created_at` are deliberately not
    # among them — see TenantModelResource.EXPORT_FIELDS for why this cannot be
    # expressed through Meta.fields alone.
    EXPORT_FIELDS = ("question", "priority", "score", "is_mandatory", "is_active")

    class Meta:
        model = ProbingQuestion
        # `id` stays importable so a file carrying one updates the row it names
        # instead of duplicating it.
        fields = (
            "id",
            "question",
            "priority",
            "score",
            "is_mandatory",
            "is_active",
        )
