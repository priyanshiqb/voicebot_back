"""Reports, for support to inspect. Never created or edited here.

A report is made by the request flow and changed only by the task that builds it. Adding
one here would produce a PENDING row no worker was ever asked to pick up, and editing a
status or a file key would point a download at something that is not what the row claims.

The page carries a freshly signed download link while the file still exists, so support
can fetch exactly what the tenant fetched instead of asking them to send it on.
"""

import logging
import uuid

from django.contrib import admin
from django.db.models import Q
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html

from core.models.campaign_report import CampaignReport
from tenant.common.admin.tenant_base_admin import TenantBaseAdmin

logger = logging.getLogger(__name__)


@admin.register(CampaignReport)
class CampaignReportAdmin(TenantBaseAdmin):
    model = CampaignReport

    # --- the list -----------------------------------------------------------

    list_display = (
        "created_at",
        "campaign",
        "tenant",
        "report_format",
        "status",
        "progress_display",
        "file_name",
        "file_size_display",
        "created_by",
        "completed_at",
        "expires_at",
    )
    list_display_links = ("created_at", "campaign")
    list_select_related = ("campaign", "tenant", "created_by")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    list_filter = (
        "status",
        "report_format",
        ("campaign", admin.RelatedOnlyFieldListFilter),
        ("file_key", admin.EmptyFieldListFilter),
        "expires_at",
    ) + TenantBaseAdmin.list_filter

    # --- searching ----------------------------------------------------------

    search_fields = (
        "file_name",
        "file_key",
        "error_message",
        "campaign__name",
        "created_by__email",
        "created_by__first_name",
        "created_by__last_name",
    )

    #: A support ticket quotes whichever id the person had to hand: the report's, its
    #: campaign's, or the requester's.
    UUID_SEARCH_FIELDS = ("pk", "tenant_id", "campaign_id", "created_by_id")

    def get_search_results(self, request, queryset, search_term):
        results, may_have_duplicates = super().get_search_results(
            request, queryset, search_term
        )
        try:
            value = uuid.UUID((search_term or "").strip())
        except (ValueError, AttributeError, TypeError):
            return results, may_have_duplicates

        matches = Q()
        for field in self.UUID_SEARCH_FIELDS:
            matches |= Q(**{field: value})
        return results | queryset.filter(matches), True

    # --- the page -----------------------------------------------------------

    readonly_fields = TenantBaseAdmin.readonly_fields + (
        "tenant",
        "campaign",
        "campaign_link",
        "report_format",
        "status",
        "file_key",
        "file_name",
        "file_size_bytes",
        "file_size_display",
        "download_link",
        "total_rows",
        "processed_rows",
        "rows_included",
        "progress_display",
        "started_at",
        "completed_at",
        "expires_at",
        "error_message",
    )

    fieldsets = (
        ("Report", {
            "fields": (
                "campaign",
                "campaign_link",
                "report_format",
                "status",
                "error_message",
            ),
        }),
        ("Progress", {
            "fields": (
                "progress_display",
                "total_rows",
                "processed_rows",
                "rows_included",
            ),
        }),
        ("File", {
            "description": (
                "The key is blank until the upload succeeds, and is cleared when the "
                "retention sweep deletes the file — so a blank key on a COMPLETED report "
                "means something removed it early."
            ),
            "fields": (
                "file_name",
                "file_key",
                "file_size_bytes",
                "file_size_display",
                "download_link",
            ),
        }),
        ("Timing", {
            "fields": ("started_at", "completed_at", "expires_at"),
        }),
        ("Record", {
            "fields": (
                "id",
                "tenant",
                "created_at",
                "updated_at",
                "created_by",
                "updated_by",
                "is_active",
                "is_deleted",
                "deleted_at",
                "deleted_by",
            ),
        }),
    )

    def has_add_permission(self, request):
        return False

    # --- computed displays --------------------------------------------------

    @admin.display(description="Progress")
    def progress_display(self, obj):
        if obj.total_rows:
            return f"{obj.progress_percent}% ({obj.processed_rows:,} of {obj.total_rows:,})"
        return f"{obj.progress_percent}%"

    @admin.display(description="Size", ordering="file_size_bytes")
    def file_size_display(self, obj):
        size = obj.file_size_bytes or 0
        if not size:
            return "—"
        if size >= 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        return f"{size / 1024:.0f} KB"

    @admin.display(description="Campaign record")
    def campaign_link(self, obj):
        if obj.campaign_id is None:
            return "—"
        try:
            url = reverse("admin:core_campaign_change", args=[obj.campaign_id])
        except NoReverseMatch:
            return obj.campaign.name
        return format_html('<a href="{}">{}</a>', url, obj.campaign.name)

    @admin.display(description="Download")
    def download_link(self, obj):
        """A freshly signed link, minted on view and never stored — the same one the
        tenant's own download endpoint issues."""
        if not obj.is_downloadable:
            return "No file to download."

        from core.services import campaign_reports as report_service

        try:
            payload = report_service.download_payload(obj)
        except Exception:
            logger.warning(
                "could not sign an admin download link for report %s",
                obj.pk, exc_info=True,
            )
            return "Could not sign a link — check the storage configuration."

        return format_html(
            '<a href="{}" target="_blank" rel="noopener">{}</a> (link valid {} minutes)',
            payload["download_url"],
            payload["file_name"],
            payload["expires_in_seconds"] // 60,
        )
