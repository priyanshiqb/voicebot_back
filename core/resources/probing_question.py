from core.models.probing_question import ProbingQuestion
from .base_resource import TenantModelResource

class ProbingQuestionResource(TenantModelResource):
    class Meta:
        model = ProbingQuestion
        fields = ('id', 'question', 'priority', 'score', 'is_mandatory', 'can_user_edit', 'is_default', 'created_at')
