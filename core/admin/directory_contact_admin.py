from django.contrib import admin

from core.models.directory_contact import DirectoryContact
from tenant.common.admin.tenant_base_admin import TenantBaseAdmin


@admin.register(DirectoryContact)
class DirectoryContactAdmin(TenantBaseAdmin):
    model = DirectoryContact

    list_display = (
        "name",
        "designation",
        "category",
        "department",
        "email",
        "phone_e164",
        "is_active",
        "created_at",
    )
    list_filter = ("category", "department") + TenantBaseAdmin.list_filter
    search_fields = (
        "name",
        "designation",
        "department",
        "email",
        "phone_number",
        "phone_e164",
    )
    ordering = ("name",)

    # Derived on save. Editable, it would drift from the raw number every other
    # surface displays.
    readonly_fields = TenantBaseAdmin.readonly_fields + ("phone_e164",)
