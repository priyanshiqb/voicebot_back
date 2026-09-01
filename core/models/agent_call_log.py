from django.db import models
from django.utils.translation import gettext_lazy as _
from django.contrib.postgres.fields import ArrayField
from core.constants.service_tier import ServiceTier
from tenant.common.models.tenant_base_model_core import TenantBaseModelCore

class AgentCallLog(TenantBaseModelCore):
    class CallDirection(models.TextChoices):
        INBOUND = "inbound", "Inbound"
        OUTBOUND = "outbound", "Outbound"

    provider_call_id = models.CharField(
        max_length=255, 
        unique=True,
        db_index=True,
        help_text=_("The unique call ID from the telephony provider.")
    )
    agent = models.ForeignKey(
        "core.Agent",
        on_delete=models.CASCADE,
        related_name="call_logs",
        help_text=_("The agent (bot) that handled this call.")
    )
    caller_number = models.CharField(
        max_length=50,
        help_text=_(
            "The number that initiated the call: the customer on inbound, the bot on "
            "outbound. NOTE: on outbound rows written before callee_number existed "
            "this holds the CUSTOMER, not the bot — see `customer_number`."
        )
    )
    callee_number = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text=_(
            "The number that received the call: the bot on inbound, the customer on "
            "outbound. Supplied by the runner — this service has no DID of its own to "
            "fall back on, since Agent carries no phone number."
        )
    )
    caller_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text=_("The name of the caller, if available.")
    )
    contact = models.ForeignKey(
        "core.Contact",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        related_name="call_logs",
        help_text=_(
            "Resolved from customer_number via E.164 matching — the caller on inbound "
            "and the callee on outbound, so an outbound call matches the person rather "
            "than the bot. SET_NULL so removing a contact never destroys call history."
        ),
    )
    direction = models.CharField(
        max_length=20,
        choices=CallDirection.choices,
        help_text=_("The direction of the call (inbound/outbound).")
    )
    campaign_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text=_(
            "Free-text campaign tag written by the runner. Left as-is deliberately: a "
            "real `campaign` FK cannot be added alongside it, because Django derives "
            "the attribute `campaign_id` from it and the two collide. Reach the "
            "campaign through `campaign_member__campaign` until this field is retired."
        )
    )
    campaign_member = models.ForeignKey(
        "core.CampaignMember",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        related_name="call_logs",
        help_text=_(
            "The member this call answers. Set explicitly rather than inferred from "
            "contact plus a time window — this log may be inbound, or belong to a "
            "different campaign running concurrently."
        ),
    )
    duration_seconds = models.PositiveIntegerField(
        default=0,
        help_text=_("The duration of the call in seconds.")
    )
    service_tier = models.CharField(
        max_length=32,
        choices=ServiceTier.choices,
        blank=True,
        null=True,
        db_index=True,
        help_text=_(
            "The tier this call was actually served on, as reported by the runner. "
            "This is what the call is billed at, because a call configured for low "
            "latency may have been served on the standard model instead. Null means "
            "the runner did not report it, and billing falls back to the agent's "
            "configured tier."
        ),
    )
    outcome = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text=_("The final outcome or disposition of the call.")
    )
    mood = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text=_("The detected mood or sentiment of the caller.")
    )
    mood_confidence = models.FloatField(
        blank=True,
        null=True,
        help_text=_("Confidence score of the mood detection (0.0 to 1.0).")
    )
    pii_redacted = models.BooleanField(
        default=False,
        help_text=_("Whether PII has been redacted from the transcript.")
    )
    ai_summary = models.TextField(
        blank=True,
        null=True,
        help_text=_("AI-generated summary of the conversation.")
    )
    transcript = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Structured JSON array containing the full conversation transcript.")
    )
    recording_file = models.ForeignKey(
        "core.AgentCallRecording",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="call_logs",
        help_text=_("The MP3 recording file uploaded directly to blob storage.")
    )
    started_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text=_("When the call actually started.")
    )
    ended_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text=_("When the call ended.")
    )
    tags = ArrayField(
        models.CharField(max_length=100, blank=True),
        default=list,
        blank=True,
        help_text=_("List of tags associated with this call.")
    )
    preferred_language = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text=_("The preferred language detected or chosen by the caller.")
    )
    probing_questions_count = models.PositiveIntegerField(
        default=0,
        help_text=_("The number of probing questions asked during the call.")
    )
    probing_questions = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Structured JSON array of probing questions and answers.")
    )
    follow_up_detail = models.TextField(
        blank=True,
        null=True,
        help_text=_("Details regarding follow-up actions required.")
    )
    human_transfer_detail = models.TextField(
        blank=True,
        null=True,
        help_text=_("Details regarding why or how the call was transferred to a human.")
    )

    class Meta:
        db_table = "core_agent_call_log"
        ordering = ["-started_at", "-created_at"]
        verbose_name = _("Agent Call Log")
        verbose_name_plural = _("Agent Call Logs")

    @property
    def customer_number(self):
        """The human's number, whichever end of the call they were on.

        Every consumer that wants "the person" — contact resolution, analytics, the
        admin — must go through this rather than reading a column directly, because
        outbound rows come in two eras and only this knows the difference:

        - Inbound, always: the customer dialled in, so it is `caller_number`.
        - Outbound, since `callee_number` shipped: the bot dialled out, so the customer
          is `callee_number`.
        - Outbound, before that: `callee_number` is NULL and `caller_number` holds the
          customer, which is why the fallback exists and why the column is nullable —
          NULL means "no era", an empty string would not.

        Keeping the fallback here and nowhere else means retiring it later is one
        deletion, once the old rows are gone or backfilled.
        """
        if self.direction == self.CallDirection.OUTBOUND:
            return self.callee_number or self.caller_number
        return self.caller_number

    def __str__(self):
        return f"{self.provider_call_id} - {self.customer_number}"
