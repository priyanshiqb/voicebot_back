from core.models.agent import Agent
from .base_resource import TenantModelResource

class AgentResource(TenantModelResource):
    class Meta:
        model = Agent
        fields = ('bot_name', 'persona_name', 'languages', 'tone', 'voice_name', 'is_active', 'created_at')
