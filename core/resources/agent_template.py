from core.models.agent_template import AgentTemplate
from .base_resource import TenantModelResource

class AgentTemplateResource(TenantModelResource):
    class Meta:
        model = AgentTemplate
        fields = ('id', 'name', 'short_description', 'industry', 'strict_facts', 'can_user_edit_flow', 'is_active', 'created_at')
