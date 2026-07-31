from django.db import models
from django.utils.translation import gettext_lazy as _
from django.contrib.postgres.fields import ArrayField
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
        help_text=_("The phone number of the caller.")
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
            "Resolved from caller_number via E.164 matching. SET_NULL so removing a "
            "contact never destroys call history."
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
        help_text=_("The MP3 recording file uploaded directly to S3.")
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

    def __str__(self):
        return f"{self.provider_call_id} - {self.caller_number}"
