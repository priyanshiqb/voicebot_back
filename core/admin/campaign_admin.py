"""The campaign as a whole: its definition, its audience, and what came of it.

Everything the row holds is on the page, and the two things a campaign is really made of
— its members and its reports — are reachable from it: reports inline, members through a
filtered link rather than an inline, because an audience runs to six figures and an inline
would try to render all of it.

The lifecycle owns `status`, the counters and the handoff fields. Hand-editing any of them
desynchronises the campaign from its members or from the runner, so they are read-only
here; what an operator defines stays editable. So does the start, until the campaign
begins, under the same rules the API applies.
"""

import uuid

from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html

from core.models.campaign import Campaign
from core.models.campaign_report import CampaignReport
from onboard.models.timezone import validate_iana_timezone
from tenant.common.admin.tenant_base_admin import TenantBaseAdmin


class CampaignReportInline(admin.TabularInline):
    """This campaign's reports, newest first, each opening on its own page.

    Read-only throughout: a report is created by the API and moved only by the job that
    builds it. A row typed in here would be a PENDING report no worker was ever asked to
    pick up.
    """

    model = CampaignReport
    extra = 0
    can_delete = False
    show_change_link = True
    ordering = ("-created_at",)
    fields = (
        "created_at",
        "report_format",
        "status",
        "progress_display",
        "file_name",
        "file_size_bytes",
        "expires_at",
        "created_by",
    )
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Progress")
    def progress_display(self, obj):
        if obj.total_rows:
            return f"{obj.progress_percent}% ({obj.processed_rows:,}/{obj.total_rows:,})"
        return f"{obj.progress_percent}%"


class CampaignAdminForm(forms.ModelForm):
    """Holds the schedule to the same rules as the API.

    The admin writes the model directly and skips the serializer, so without this an
    operator could set a start in the past (the sweep launches it within a minute), clear
    a scheduled campaign's start (it then never launches), or store a timezone nothing
    can render (every time in its report fails).
    """

    class Meta:
        model = Campaign
        fields = "__all__"

    def clean_schedule_timezone(self):
        value = self.cleaned_data.get("schedule_timezone")
        if value:
            try:
                validate_iana_timezone(value)
            except ValidationError as exc:
                raise forms.ValidationError(exc.messages)
        return value

    def clean_scheduled_start_at(self):
        value = self.cleaned_data.get("scheduled_start_at")
        # Only a changed start is checked. A campaign whose start passed a moment ago is
        # about to be launched, and saving an unrelated edit to it must not fail on that.
        if "scheduled_start_at" in self.changed_data:
            try:
                # The model's own rule, not a copy of it: future-only, and a SCHEDULED
                # campaign cannot be left without a start.
                self.instance.set_start_instant(value)
            except ValidationError as exc:
                raise forms.ValidationError(exc.message_dict["scheduled_start_at"])
        return value


@admin.register(Campaign)
class CampaignAdmin(TenantBaseAdmin):
    model = Campaign

    form = CampaignAdminForm
    inlines = (CampaignReportInline,)

    # --- the list -----------------------------------------------------------

    list_display = (
        "name",
        "tenant",
        "agent",
        "status",
        "handoff_status",
        "scheduled_start_at",
        "total_members",
        "pending_count",
        "suppressed_count",
        "completed_count",
        "failed_count",
        "created_at",
    )
    list_display_links = ("name",)
    list_select_related = ("tenant", "agent")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    list_filter = (
        "status",
        "audience_source",
        "handoff_status",
        ("agent", admin.RelatedOnlyFieldListFilter),
        "scheduled_start_at",
        "started_at",
    ) + TenantBaseAdmin.list_filter

    # --- searching ----------------------------------------------------------

    search_fields = (
        "name",
        "description",
        "error_message",
        "schedule_timezone",
        "agent__bot_name",
        "launched_by__email",
        "members__phone_e164",
        "members__contact__first_name",
        "members__contact__last_name",
    )

    #: Paste any id that belongs to the campaign and land on it — including a member id
    #: or a report id, which are the ones a report or a support ticket hands out.
    UUID_SEARCH_FIELDS = (
        "pk",
        "tenant_id",
        "agent_id",
        "handoff_id",
        "launched_by_id",
        "members__id",
        "members__contact_id",
        "reports__id",
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
        # Joining through members and reports can repeat a campaign; the admin
        # de-duplicates when told the search may.
        return (results | queryset.filter(matches)).distinct(), True

    # --- the page -----------------------------------------------------------

    filter_horizontal = ("contacts",)
    autocomplete_fields = TenantBaseAdmin.autocomplete_fields + ("agent",)

    # Every one of these is derived by the campaign lifecycle. Hand-editing status or a
    # counter desynchronises the campaign from its members; hand-editing the handoff
    # fields desynchronises it from the runner. The schedule fields are the exception
    # while the campaign has not begun: see get_readonly_fields().
    readonly_fields = TenantBaseAdmin.readonly_fields + (
        "status",
        "scheduled_start_at",
        "schedule_timezone",
        "local_start_display",
        "started_at",
        "completed_at",
        "elapsed_display",
        "launched_by",
        "paused_by",
        "cancelled_by",
        "handoff_id",
        "handoff_status",
        "handed_off_at",
        "total_members",
        "pending_count",
        "suppressed_count",
        "completed_count",
        "failed_count",
        "error_message",
        "members_link",
        "reports_link",
    )

    fieldsets = (
        ("Definition", {
            "fields": ("name", "description", "agent", "is_active"),
        }),
        ("Audience", {
            "fields": (
                "audience_source",
                "contacts",
                "audience_filter",
                "members_link",
            ),
        }),
        ("Schedule", {
            "description": (
                "Enter the start in <strong>UTC</strong> (the server's time): 13:00 in "
                "India is 07:30 UTC. <em>Starts (campaign timezone)</em> shows the saved "
                "start in the campaign's own timezone. Editable only while the campaign "
                "is Draft or Scheduled; a scheduled campaign launches within a minute of "
                "its start."
            ),
            "fields": (
                "scheduled_start_at",
                "schedule_timezone",
                "local_start_display",
                "started_at",
                "completed_at",
                "elapsed_display",
            ),
        }),
        ("Handoff", {
            "description": (
                "Set by the push handoff to the voice runner. PENDING with a campaign "
                "that has been RUNNING for a while means the runner never acknowledged."
            ),
            "fields": ("handoff_status", "handoff_id", "handed_off_at"),
        }),
        ("Progress", {
            "fields": (
                "status",
                "total_members",
                "pending_count",
                "suppressed_count",
                "completed_count",
                "failed_count",
                "error_message",
            ),
        }),
        ("Reports", {
            "fields": ("reports_link",),
        }),
        ("Attribution", {
            "fields": ("launched_by", "paused_by", "cancelled_by"),
        }),
        ("Record", {
            "fields": (
                "id",
                "tenant",
                "created_at",
                "updated_at",
                "created_by",
                "updated_by",
                "is_deleted",
                "deleted_at",
                "deleted_by",
            ),
        }),
    )

    #: The start may move until the campaign begins — the same statuses the API allows.
    #: After that, moving it would only rewrite the record of when the campaign started.
    SCHEDULE_EDITABLE_STATUSES = (Campaign.Status.DRAFT, Campaign.Status.SCHEDULED)
    SCHEDULE_FIELDS = ("scheduled_start_at", "schedule_timezone")

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if obj is None or obj.status in self.SCHEDULE_EDITABLE_STATUSES:
            return tuple(field for field in fields if field not in self.SCHEDULE_FIELDS)
        return fields

    # --- computed displays --------------------------------------------------

    @admin.display(description="Members")
    def members_link(self, obj):
        """A link, not an inline: an audience can run to a hundred thousand rows."""
        if obj.pk is None:
            return "—"
        try:
            url = reverse("admin:core_campaignmember_changelist")
        except NoReverseMatch:
            return "—"
        return format_html(
            '<a href="{}?campaign__id__exact={}">View {} members</a>',
            url, obj.pk, f"{obj.total_members:,}",
        )

    @admin.display(description="All reports")
    def reports_link(self, obj):
        if obj.pk is None:
            return "—"
        try:
            url = reverse("admin:core_campaignreport_changelist")
        except NoReverseMatch:
            return "—"
        count = obj.reports.count()
        return format_html(
            '<a href="{}?campaign__id__exact={}">View {} reports</a>', url, obj.pk, count
        )

    @admin.display(description="Starts (campaign timezone)")
    def local_start_display(self, obj):
        """`scheduled_start_at` is stored in UTC; this is the wall clock it was typed as."""
        local = obj.local_start
        return local.strftime("%d %b %Y %H:%M (%Z)") if local else "Not scheduled"

    @admin.display(description="Elapsed")
    def elapsed_display(self, obj):
        if not obj.started_at:
            return "Not started"
        if not obj.completed_at:
            return "Still open"
        seconds = int((obj.completed_at - obj.started_at).total_seconds())
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours}h {minutes:02d}m {secs:02d}s" if hours else f"{minutes}m {secs:02d}s"
