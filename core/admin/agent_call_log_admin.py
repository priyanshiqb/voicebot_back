from django.contrib import admin
from tenant.common.admin.tenant_base_admin_core import TenantBaseAdminCore
from core.models.agent_call_log import AgentCallLog

@admin.register(AgentCallLog)
class AgentCallLogAdmin(TenantBaseAdminCore):
    list_display = (
        "provider_call_id",
        "agent",
        "caller_number",
        "callee_number",
        "direction",
        "duration_seconds",
        "outcome",
        "started_at",
    )
    list_filter = ("direction", "outcome", "agent")
    # Both legs are searchable: customer_number is a Python property and cannot be
    # queried, so searching for a person's number must hit whichever column holds it.
    search_fields = (
        "provider_call_id",
        "caller_number",
        "callee_number",
        "caller_name",
        "id",
    )
    autocomplete_fields = ("agent",)

    def has_add_permission(self, request):
        # Call logs should ideally be ingested via API, not created manually
        return False
