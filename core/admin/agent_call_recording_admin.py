from django.contrib import admin
from tenant.common.admin.tenant_base_admin_core import TenantBaseAdminCore
from core.models.agent_call_recording import AgentCallRecording

@admin.register(AgentCallRecording)
class AgentCallRecordingAdmin(TenantBaseAdminCore):
    list_display = ("id", "tenant", "original_name", "extension", "file_size_in_bytes", "is_processed", "created_at")
    search_fields = ("id", "original_name")
    list_filter = ("is_processed", "extension", "created_at")
