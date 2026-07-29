from django.contrib import admin

from core.models.contact_import import (
    ContactImportFile,
    ContactImportRow,
    ContactImportSession,
)
from tenant.common.admin.tenant_base_admin_core import TenantBaseAdminCore


class ReadOnlyStagingAdmin(TenantBaseAdminCore):
    """
    Staging tables are machine-managed. Hand-editing a row or session corrupts the
    state machine the parse and commit passes rely on, so the admin is view-only.
    """

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ContactImportFile)
class ContactImportFileAdmin(ReadOnlyStagingAdmin):
    model = ContactImportFile

    list_display = (
        "id",
        "original_name",
        "tenant",
        "upload_status",
        "file_size_in_bytes",
        "created_by",
        "created_at",
    )
    list_filter = ("upload_status",) + TenantBaseAdminCore.list_filter
    search_fields = ("original_name",)
    ordering = ("-created_at",)


@admin.register(ContactImportSession)
class ContactImportSessionAdmin(ReadOnlyStagingAdmin):
    model = ContactImportSession

    list_display = (
        "id",
        "tenant",
        "status",
        "total_rows",
        "conflict_rows",
        "invalid_rows",
        "created_count",
        "updated_count",
        "skipped_count",
        "failed_count",
        "created_by",
        "committed_by",
        "created_at",
    )
    list_filter = ("status", "conflict_strategy") + TenantBaseAdminCore.list_filter
    ordering = ("-created_at",)


@admin.register(ContactImportRow)
class ContactImportRowAdmin(ReadOnlyStagingAdmin):
    model = ContactImportRow

    list_display = (
        "id",
        "session",
        "row_number",
        "phone_e164",
        "status",
        "resolution",
        "updated_by",
    )
    list_filter = ("status", "resolution") + TenantBaseAdminCore.list_filter
    search_fields = ("phone_e164",)
    ordering = ("session", "row_number")
