"""One person in one campaign: what was frozen about them, and what came back.

View-only. Members are derived from the campaign audience and closed out by the runner's
updates; hand-editing a row would desynchronise the campaign counters and, for a
suppressed row, silently discard a compliance record.

The page also lists the call logs attached to the member — the join that campaign reports
read. An empty list there is the reason a report shows blank call columns, so it is worth
seeing directly rather than inferring.
"""

import uuid

from django.contrib import admin
from django.db.models import Q
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from core.models.campaign_member import CampaignMember
from tenant.common.admin.tenant_base_admin_core import TenantBaseAdminCore

#: Calls listed on the page. A member should have one; a retried runner can post more,
#: and a handful is enough to see what happened without the page growing unbounded.
CALL_LOG_LIMIT = 20


@admin.register(CampaignMember)
class CampaignMemberAdmin(TenantBaseAdminCore):
    model = CampaignMember

    # --- the list -----------------------------------------------------------

    list_display = (
        "created_at",
        "campaign",
        "contact",
        "phone_e164",
        "status",
        "suppression_reason",
        "outcome",
        "attempt_count",
        "last_call_at",
        "has_call_log",
    )
    list_display_links = ("created_at", "contact")
    list_select_related = ("campaign", "contact")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    list_filter = (
        "status",
        "suppression_reason",
        ("campaign", admin.RelatedOnlyFieldListFilter),
        ("last_call_at", admin.EmptyFieldListFilter),
        ("outcome", admin.EmptyFieldListFilter),
        "last_call_at",
    ) + TenantBaseAdminCore.list_filter

    # --- searching ----------------------------------------------------------

    search_fields = (
        "phone_e164",
        "outcome",
        "error_message",
        "external_ref",
        "campaign__name",
        "contact__first_name",
        "contact__last_name",
        "contact__phone_e164",
        "contact__email_address",
        "contact__company",
        "call_logs__provider_call_id",
    )

    #: A member id is what a campaign report puts in front of people; a call log id or a
    #: contact id is what a support ticket quotes.
    UUID_SEARCH_FIELDS = (
        "pk",
        "tenant_id",
        "campaign_id",
        "contact_id",
        "call_logs__id",
    )

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
        return (results | queryset.filter(matches)).distinct(), True

    # --- the page -----------------------------------------------------------

    readonly_fields = TenantBaseAdminCore.readonly_fields + (
        "tenant",
        "campaign",
        "campaign_link",
        "contact",
        "contact_link",
        "phone_e164",
        "status",
        "suppression_reason",
        "outcome",
        "error_message",
        "attempt_count",
        "last_call_at",
        "external_ref",
        "call_logs_display",
    )

    fieldsets = (
        ("Identity", {
            "fields": ("id", "tenant", "campaign", "campaign_link"),
        }),
        ("The person", {
            "description": (
                "`phone_e164` is frozen at materialisation — the number actually handed "
                "to the runner, whatever the contact's number says today."
            ),
            "fields": ("contact", "contact_link", "phone_e164"),
        }),
        ("What happened", {
            "fields": (
                "status",
                "suppression_reason",
                "outcome",
                "error_message",
                "attempt_count",
                "last_call_at",
                "external_ref",
            ),
        }),
        ("Calls", {
            "description": (
                "A call is attached when the runner's member update carries its "
                "provider_call_id. Nothing here means campaign reports show blank call "
                "columns for this person."
            ),
            "fields": ("call_logs_display",),
        }),
        ("Record", {
            "fields": ("created_at", "updated_at", "is_active", "is_deleted", "deleted_at"),
        }),
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    # --- computed displays --------------------------------------------------

    @admin.display(boolean=True, description="Call log")
    def has_call_log(self, obj):
        return obj.call_logs.exists()

    @admin.display(description="Campaign record")
    def campaign_link(self, obj):
        if obj.campaign_id is None:
            return "—"
        return self._admin_link("campaign", obj.campaign_id, obj.campaign.name)

    @admin.display(description="Contact record")
    def contact_link(self, obj):
        if obj.contact_id is None:
            return "—"
        return self._admin_link("contact", obj.contact_id, str(obj.contact))

    @admin.display(description="Call logs")
    def call_logs_display(self, obj):
        logs = list(obj.call_logs.order_by("-created_at")[:CALL_LOG_LIMIT])
        if not logs:
            # A constant, so mark_safe rather than format_html, which requires arguments.
            return mark_safe(
                "<strong>No call log is attached to this member.</strong> Campaign "
                "reports will show blank call columns for this row."
            )

        rows = []
        for log in logs:
            credits = "—" if log.credits_charged is None else f"{log.credits_charged}"
            rows.append((
                self._admin_link(
                    "agentcalllog", log.pk, log.provider_call_id or str(log.pk)
                ),
                f"{log.duration_seconds or 0}s",
                credits,
                log.outcome or "—",
                log.created_at.strftime("%d %b %Y %H:%M"),
            ))
        return format_html(
            '<table style="margin:0"><tr><th>Call</th><th>Duration</th>'
            "<th>Credits</th><th>Outcome</th><th>Logged</th></tr>{}</table>",
            format_html_join(
                "",
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                rows,
            ),
        )

    @staticmethod
    def _admin_link(model, pk, label):
        try:
            url = reverse(f"admin:core_{model}_change", args=[pk])
        except NoReverseMatch:
            return str(label)
        return format_html('<a href="{}">{}</a>', url, label)
