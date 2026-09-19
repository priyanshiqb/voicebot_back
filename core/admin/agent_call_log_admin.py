"""Everything one call recorded, on one page.

Support opens a call log to answer "what actually happened on this call?", so the page
shows **every** column the row has — including the JSON the runner posted — rather than a
curated subset. `core/tests/admin/test_agent_call_log_admin.py` fails if a field is added
to the model without a home here.

Call logs arrive through the API and are never typed in, so identity, timing and billing
are read-only: those are the runner's record and the billing record. What an operator may
legitimately repair stays editable — which contact or campaign member a call belongs to,
its disposition, its redaction flag.
"""

import json
import uuid

from django.contrib import admin
from django.db.models import Q
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html

from core.models.agent_call_log import AgentCallLog
from tenant.common.admin.tenant_base_admin_core import TenantBaseAdminCore

#: A pretty-printed payload longer than this is cut. The page is for reading; a
#: forty-minute call's transcript is not, and the report carries it in full anyway.
JSON_PREVIEW_LIMIT = 20000

#: Fields shown through a rendered block rather than their raw widget. The admin test
#: reads this, so adding a JSON field to the model forces a decision about its display
#: rather than letting it drop off the page silently.
JSON_DISPLAY_FIELDS = {
    "transcript": "transcript_display",
    "probing_questions": "probing_questions_display",
}


@admin.register(AgentCallLog)
class AgentCallLogAdmin(TenantBaseAdminCore):
    # --- the list -----------------------------------------------------------

    list_display = (
        "created_at",
        "provider_call_id",
        "agent",
        "direction",
        "customer_number_display",
        "duration_display",
        "outcome",
        "mood",
        "credits_charged",
        "is_linked_to_member",
        "has_recording",
    )
    list_display_links = ("created_at", "provider_call_id")
    list_select_related = ("agent", "contact", "campaign_member", "recording_file")
    date_hierarchy = "created_at"

    list_filter = (
        "direction",
        "service_tier",
        "mood",
        "pii_redacted",
        # "Empty" here answers the question support actually asks: which calls never got
        # attached to a campaign member, were never charged, or have no recording.
        ("campaign_member", admin.EmptyFieldListFilter),
        ("credits_charged", admin.EmptyFieldListFilter),
        ("recording_file", admin.EmptyFieldListFilter),
        ("agent", admin.RelatedOnlyFieldListFilter),
        "started_at",
    ) + TenantBaseAdminCore.list_filter

    # --- searching ----------------------------------------------------------

    #: Text search. `id` is deliberately **not** here: as a search field it casts every
    #: row's UUID to text and scans, and it only ever matched this table's own id. UUIDs
    #: are handled exactly, and across relations, in `get_search_results`.
    search_fields = (
        "provider_call_id",
        "caller_number",
        "callee_number",
        "caller_name",
        "outcome",
        "mood",
        "campaign_id",
        "preferred_language",
        "ai_summary",
        "follow_up_detail",
        "human_transfer_detail",
        "external_recording_url",
        "contact__first_name",
        "contact__last_name",
        "contact__phone_e164",
        "contact__email_address",
        "agent__bot_name",
        "campaign_member__phone_e164",
        "campaign_member__campaign__name",
    )

    #: Paste any of these ids and the call turns up. People arrive holding whatever id
    #: the screen in front of them showed: a campaign report hands out Member IDs and
    #: provider call ids, a support ticket quotes a contact id.
    UUID_SEARCH_FIELDS = (
        "pk",
        "agent_id",
        "contact_id",
        "campaign_member_id",
        "recording_file_id",
        "campaign_member__campaign_id",
        "tenant_id",
    )

    def get_search_results(self, request, queryset, search_term):
        """Text search as usual, plus an exact match when the term is a UUID.

        Exact, not `icontains`: a UUID column is indexed, and casting every row to text
        to run a LIKE over it is the slow way to answer a question that has one answer.
        """
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

    raw_id_fields = ("contact", "campaign_member", "recording_file")

    readonly_fields = TenantBaseAdminCore.readonly_fields + (
        "tenant",
        "provider_call_id",
        "agent",
        "direction",
        "caller_number",
        "callee_number",
        "customer_number_display",
        "campaign_id",
        "campaign_link",
        "member_link",
        "contact_link",
        "started_at",
        "ended_at",
        "duration_seconds",
        "duration_display",
        "service_tier",
        "credits_charged",
        "probing_questions_count",
        "transcript_display",
        "probing_questions_display",
        "recording_display",
    )

    fieldsets = (
        ("Identity", {
            "fields": (
                "id",
                "provider_call_id",
                "tenant",
                "agent",
                "direction",
                "campaign_id",
            ),
        }),
        ("Who was on the call", {
            "fields": (
                "caller_number",
                "callee_number",
                "customer_number_display",
                "caller_name",
                "contact",
                "contact_link",
            ),
        }),
        ("Campaign", {
            "description": (
                "A call is attached to a member only when the runner's member update "
                "carries its provider_call_id. An unlinked call is invisible to campaign "
                "reports — set the member here to repair one."
            ),
            "fields": ("campaign_member", "member_link", "campaign_link"),
        }),
        ("Timing", {
            "fields": (
                "started_at",
                "ended_at",
                "duration_seconds",
                "duration_display",
                "created_at",
                "updated_at",
            ),
        }),
        ("Billing", {
            "fields": ("service_tier", "credits_charged"),
        }),
        ("Outcome and analysis", {
            "fields": (
                "outcome",
                "mood",
                "mood_confidence",
                "preferred_language",
                "tags",
                "follow_up_detail",
                "human_transfer_detail",
                "pii_redacted",
            ),
        }),
        ("What was said", {
            "fields": (
                "ai_summary",
                "probing_questions_count",
                "probing_questions_display",
                "transcript_display",
            ),
        }),
        ("Recording", {
            "fields": ("recording_file", "recording_display", "external_recording_url"),
        }),
        ("Record state", {
            "fields": ("is_active", "is_deleted", "deleted_at"),
        }),
    )

    def has_add_permission(self, request):
        # Call logs are ingested through the API. One typed in by hand would have no
        # provider call id anybody could reconcile against.
        return False

    # --- computed displays --------------------------------------------------

    @admin.display(description="Customer number")
    def customer_number_display(self, obj):
        """The human's number, whichever leg they were on — a property, so unsearchable
        and worth showing rather than leaving the reader to work out the direction."""
        return obj.customer_number or "—"

    @admin.display(description="Duration", ordering="duration_seconds")
    def duration_display(self, obj):
        seconds = obj.duration_seconds or 0
        if not seconds:
            return "0s (never connected)"
        minutes, remainder = divmod(int(seconds), 60)
        return f"{minutes}m {remainder:02d}s" if minutes else f"{remainder}s"

    @admin.display(boolean=True, description="On a campaign")
    def is_linked_to_member(self, obj):
        return obj.campaign_member_id is not None

    @admin.display(boolean=True, description="Recording")
    def has_recording(self, obj):
        return bool(obj.recording_file_id or obj.external_recording_url)

    @admin.display(description="Contact record")
    def contact_link(self, obj):
        if obj.contact_id is None:
            return "—"
        return self._admin_link("contact", obj.contact_id, str(obj.contact))

    @admin.display(description="Member record")
    def member_link(self, obj):
        if obj.campaign_member_id is None:
            return "Not linked to any campaign member"
        return self._admin_link(
            "campaignmember", obj.campaign_member_id, str(obj.campaign_member)
        )

    @admin.display(description="Campaign")
    def campaign_link(self, obj):
        campaign = getattr(obj.campaign_member, "campaign", None)
        if campaign is None:
            return "—"
        return self._admin_link("campaign", campaign.pk, campaign.name)

    @admin.display(description="Stored recording")
    def recording_display(self, obj):
        recording = obj.recording_file
        if recording is None:
            return "—"
        name = getattr(getattr(recording, "file", None), "name", "")
        label = name or recording.original_name or str(recording.pk)
        size = recording.file_size_in_bytes or 0
        return self._admin_link(
            "agentcallrecording", recording.pk, f"{label} ({size:,} bytes)"
        )

    @admin.display(description="Probing questions")
    def probing_questions_display(self, obj):
        return self._json_block(obj.probing_questions)

    @admin.display(description="Transcript")
    def transcript_display(self, obj):
        return self._json_block(obj.transcript)

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def _admin_link(model, pk, label):
        try:
            url = reverse(f"admin:core_{model}_change", args=[pk])
        except NoReverseMatch:
            return str(label)
        return format_html('<a href="{}">{}</a>', url, label)

    @staticmethod
    def _json_block(value):
        """Runner-written JSON, pretty-printed and never allowed to break the page."""
        if value in (None, "", [], {}):
            return "—"
        try:
            text = json.dumps(value, indent=2, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)

        if len(text) > JSON_PREVIEW_LIMIT:
            text = (
                text[:JSON_PREVIEW_LIMIT]
                + f"\n… {len(text) - JSON_PREVIEW_LIMIT:,} more characters"
            )
        return format_html(
            '<pre style="max-height:420px;overflow:auto;white-space:pre-wrap;'
            'margin:0;padding:8px;background:#f8f8f8;border:1px solid #ddd">{}</pre>',
            text,
        )
