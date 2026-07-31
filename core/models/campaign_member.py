from django.db import models
from django.utils.translation import gettext_lazy as _

from core.models.contact import Contact
from tenant.common.models.tenant_base_model_core import TenantBaseModelCore


def suppression_reason_for(contact):
    """
    Why this contact must not be called, or None if they may be.

    Order matters: do_not_call is a hard suppression and is reported ahead of a consent
    state, because "they asked us not to call" is the more specific answer.

    ConsentStatus.UNKNOWN deliberately does NOT suppress. Every contact starts UNKNOWN,
    so suppressing it would make every campaign call nobody. Whether an unknown consent
    state should block dialling is a compliance policy question, not an engineering
    default — if the answer becomes yes, it belongs on Campaign as an explicit flag.
    """
    if contact.is_deleted:
        return CampaignMember.SuppressionReason.CONTACT_DELETED
    if contact.do_not_call:
        return CampaignMember.SuppressionReason.DO_NOT_CALL
    if contact.consent_status in (
        Contact.ConsentStatus.DENIED,
        Contact.ConsentStatus.WITHDRAWN,
    ):
        return CampaignMember.SuppressionReason.CONSENT_DENIED
    if not contact.phone_e164:
        return CampaignMember.SuppressionReason.NO_PHONE
    return None


class CampaignMember(TenantBaseModelCore):
    """
    One contact's place in one campaign. Tier 1 — machine-managed, never hand-edited.

    Ownership split: the external runner receives the whole campaign, builds its own
    call queue, and dials. This table is *not* that queue. It is our record of what we
    handed over and our reconciliation point for the per-member updates the runner
    sends back.

    It therefore serves three purposes at once:

    1. The frozen snapshot of who was in scope, and on which number, at handoff time.
    2. The compliance record of who was deliberately excluded and why (SUPPRESSED rows
       are never handed to the runner at all).
    3. The landing place for runner updates, so "was this contact reached" is answerable
       without joining against call logs.

    Campaign.contacts (the M2M) cannot do any of this — it is two foreign keys with no
    room for a reason, an outcome, or the number as it stood at handoff. It also changes
    retroactively: drop a contact from the audience and the row disappears, taking the
    history with it.
    """

    class Status(models.TextChoices):
        # Ours to write, at materialisation.
        PENDING = "PENDING", "Handed over, awaiting outcome"
        SUPPRESSED = "SUPPRESSED", "Excluded, never handed over"
        # Ours to write, on operator action.
        CANCELLED = "CANCELLED", "Cancelled before it was reached"
        # Mirrored from runner updates.
        IN_PROGRESS = "IN_PROGRESS", "Runner reported dialling"
        COMPLETED = "COMPLETED", "Runner reported a finished call"
        FAILED = "FAILED", "Runner reported failure"

    class SuppressionReason(models.TextChoices):
        DO_NOT_CALL = "DO_NOT_CALL", "On the do-not-call list"
        CONSENT_DENIED = "CONSENT_DENIED", "Consent denied or withdrawn"
        NO_PHONE = "NO_PHONE", "No usable phone number"
        CONTACT_DELETED = "CONTACT_DELETED", "Contact was deleted"

    # Written by us at materialisation; the runner never sees SUPPRESSED rows.
    LOCAL_STATUSES = [Status.PENDING, Status.SUPPRESSED, Status.CANCELLED]
    # Only ever set by reconciling an inbound runner update.
    RUNNER_STATUSES = [Status.IN_PROGRESS, Status.COMPLETED, Status.FAILED]
    # Statuses from which no further work happens.
    TERMINAL_STATUSES = [
        Status.SUPPRESSED,
        Status.CANCELLED,
        Status.COMPLETED,
        Status.FAILED,
    ]

    campaign = models.ForeignKey(
        "core.Campaign",
        on_delete=models.CASCADE,
        related_name="members",
        db_index=True,
    )
    contact = models.ForeignKey(
        "core.Contact",
        on_delete=models.PROTECT,
        related_name="campaign_memberships",
    )
    phone_e164 = models.CharField(
        max_length=20,
        blank=True,
        db_index=True,
        help_text=_(
            "The number as of materialisation. Frozen deliberately: if the contact's "
            "number is edited later, the campaign still records what was handed over."
        ),
    )

    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.PENDING,
        help_text=_("Server-derived. Never accepted from a client."),
    )
    suppression_reason = models.CharField(
        max_length=20,
        choices=SuppressionReason.choices,
        blank=True,
        help_text=_(
            "Populated only for SUPPRESSED. Kept distinct from FAILED because "
            "'we refused to call them' and 'the runner tried and it failed' are "
            "different answers to a regulator."
        ),
    )

    # --- Mirrored from runner updates. Not a source of truth. -----------------
    attempt_count = models.PositiveIntegerField(
        default=0,
        help_text=_(
            "How many times the runner reported attempting this member. Mirrored, not "
            "authoritative — the runner owns its own retry policy. Stored rather than "
            "derived so campaign reporting does not have to join call logs."
        ),
    )
    last_call_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(
        max_length=200,
        blank=True,
        help_text=_("Runner-supplied disposition, stored verbatim."),
    )
    external_ref = models.CharField(
        max_length=128,
        blank=True,
        db_index=True,
        help_text=_(
            "The runner's own identifier for this member, if it issues one. Nullable "
            "because it may instead echo back our id — but without somewhere to put "
            "it, correlating their records with ours during an incident is guesswork."
        ),
    )
    error_message = models.TextField(blank=True)

    class Meta:
        db_table = "core_campaign_member"
        verbose_name = "Campaign Member"
        verbose_name_plural = "Campaign Members"
        ordering = ["created_at"]
        constraints = [
            # Last line of defence against handing the runner the same person twice —
            # including when a materialisation task is retried after a crash. Also what
            # makes (campaign, phone_e164) unique, which update matching relies on when
            # the runner reports by number rather than by id.
            models.UniqueConstraint(
                fields=["campaign", "contact"],
                condition=models.Q(is_deleted=False),
                name="unique_active_campaign_member",
            ),
        ]
        indexes = [
            models.Index(fields=["campaign", "status"]),
        ]

    def __str__(self):
        return f"{self.phone_e164 or self.contact_id} ({self.status})"

    @property
    def is_suppressed(self):
        return self.status == self.Status.SUPPRESSED

    @property
    def is_terminal(self):
        return self.status in self.TERMINAL_STATUSES

    @property
    def was_handed_off(self):
        """Suppressed members are filtered out before the handoff; everything else went."""
        return self.status != self.Status.SUPPRESSED

    @property
    def was_contacted(self):
        """
        True once the runner has reported a finished call.

        Deliberately keyed off COMPLETED rather than "has any attempts", so a member the
        runner tried three times and gave up on does not read as contacted.
        """
        return self.status == self.Status.COMPLETED
