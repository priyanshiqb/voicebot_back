from django.contrib import admin

from core.models.campaign_member import CampaignMember
from tenant.common.admin.tenant_base_admin_core import TenantBaseAdminCore


@admin.register(CampaignMember)
class CampaignMemberAdmin(TenantBaseAdminCore):
    """
    View-only. Members are derived from the campaign audience and closed out by
    incoming call logs; hand-editing a row would desynchronise the campaign counters
    and, for a suppressed row, silently discard a compliance record.
    """

    model = CampaignMember

    list_display = (
        "id",
        "campaign",
        "contact",
        "phone_e164",
        "status",
        "suppression_reason",
        "attempt_count",
        "last_call_at",
        "created_at",
    )
    list_filter = ("status", "suppression_reason") + TenantBaseAdminCore.list_filter
    search_fields = (
        "phone_e164",
        "campaign__name",
        "contact__first_name",
        "contact__last_name",
    )
    ordering = ("-created_at",)
    list_select_related = ("campaign", "contact")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
