from core.models.probing_question import ProbingQuestion
from .base_resource import TenantModelResource

class ProbingQuestionResource(TenantModelResource):
    class Meta:
        model = ProbingQuestion
        fields = ('id', 'question', 'priority', 'score', 'is_mandatory', 'is_active', 'created_at')
