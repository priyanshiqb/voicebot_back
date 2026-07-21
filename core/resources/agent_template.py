from core.models.agent_template import AgentTemplate
from .base_resource import TenantModelResource

class AgentTemplateResource(TenantModelResource):
    class Meta:
        model = AgentTemplate
        fields = ('id', 'name', 'short_description', 'visual_icon', 'primary_accent', 'strict_facts', 'can_user_edit_flow', 'industry', 'created_at')
