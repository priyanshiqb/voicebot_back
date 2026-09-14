from django.contrib import admin

from core.models.campaign_report import CampaignReport
from tenant.common.admin.tenant_base_admin import TenantBaseAdmin


@admin.register(CampaignReport)
class CampaignReportAdmin(TenantBaseAdmin):
    """Reports, for support to inspect. Never created or edited here.

    A report is made by the request flow and changed only by the task that builds it.
    Adding one here would produce a PENDING row no worker was ever asked to pick up, and
    editing a status or a file key would point a download at something that is not
    what the row claims.
    """

    model = CampaignReport

    list_display = (
        "id",
        "campaign",
        "tenant",
        "report_format",
        "status",
        "processed_rows",
        "total_rows",
        "created_by",
        "created_at",
        "completed_at",
        "expires_at",
    )
    list_filter = ("status", "report_format") + TenantBaseAdmin.list_filter
    search_fields = ("campaign__name", "file_name", "error_message")
    ordering = ("-created_at",)

    readonly_fields = TenantBaseAdmin.readonly_fields + (
        "campaign",
        "report_format",
        "status",
        "file_key",
        "file_name",
        "file_size_bytes",
        "total_rows",
        "processed_rows",
        "rows_included",
        "started_at",
        "completed_at",
        "expires_at",
        "error_message",
    )

    fieldsets = (
        ("Report", {"fields": ("campaign", "report_format", "status", "error_message")}),
        ("Progress", {"fields": ("total_rows", "processed_rows", "rows_included")}),
        ("File", {"fields": ("file_name", "file_key", "file_size_bytes")}),
        ("Timing", {"fields": ("started_at", "completed_at", "expires_at")}),
    )

    def has_add_permission(self, request):
        return False
