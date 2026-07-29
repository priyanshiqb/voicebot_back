from django.contrib import admin
from tenant.common.admin.tenant_base_admin import TenantBaseAdmin
from core.models.custom_voice import CustomVoice

@admin.register(CustomVoice)
class CustomVoiceAdmin(TenantBaseAdmin):
    list_display = ("id", "tenant", "get_name", "is_active", "created_at")
    search_fields = ("id",)
    
    def get_name(self, obj):
        return obj.data.get("name", "N/A")
    get_name.short_description = "Name"

    fieldsets = (
        ("Core Details", {
            "fields": ("id", "tenant", "data", "is_active")
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at", "deleted_at")
        }),
    )
    readonly_fields = ("id", "created_at", "updated_at", "deleted_at")
