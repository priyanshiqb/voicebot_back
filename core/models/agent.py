from django.core.validators import MinLengthValidator, MaxLengthValidator, RegexValidator
from django.db import models

from core.constants.service_tier import ServiceTier
from core.models.campaign import Campaign
from tenant.common.models.tenant_base_model import TenantBaseModel


class Agent(TenantBaseModel):
    """
    Model representing Voicebot Agents configured for a tenant.
    Fully multi-tenant, audited, and soft-delete compatible.
    """
    class ToneChoices(models.TextChoices):
        PROFESSIONAL = "PROFESSIONAL", "Professional"
        FRIENDLY = "FRIENDLY", "Friendly"
        FORMAL = "FORMAL", "Formal"
        CASUAL = "CASUAL", "Casual"
        ENTHUSIASTIC = "ENTHUSIASTIC", "Enthusiastic"
        RUDE = "RUDE", "Rude"

    bot_name = models.CharField(
        max_length=100,
        validators=[
            MinLengthValidator(2),
            MaxLengthValidator(100),
            RegexValidator(
                regex=r"^[\w\s-]+$",
                message="Bot Name can only contain letters, numbers, spaces, hyphens, and underscores.",
            ),
        ],
        help_text="Name of the bot.",
    )
    persona_name = models.CharField(
        max_length=100,
        validators=[
            MinLengthValidator(2),
            MaxLengthValidator(100),
            RegexValidator(
                regex=r"^[\w\s-]+$",
                message="Persona Name can only contain letters, numbers, spaces, hyphens, and underscores.",
            ),
        ],
        help_text="Name of the persona the bot adopts.",
    )
    languages = models.JSONField(
        default=list,
        help_text="List of languages supported by the agent (e.g., ['en-IN']).",
    )
    tone = models.CharField(
        max_length=50,
        choices=ToneChoices.choices,
        default=ToneChoices.PROFESSIONAL,
        help_text="Tone of the agent interaction.",
    )
    agent_template = models.ForeignKey(
        "core.AgentTemplate",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="agents",
        help_text="The selected template used to configure this agent.",
    )
    voice_name = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Identifier for the chosen agent voice.",
    )
    service_tier = models.CharField(
        max_length=32,
        choices=ServiceTier.choices,
        default=ServiceTier.CONVERSATION_HIGH_LATENCY,
        db_index=True,
        help_text=(
            "How this bot is served, and therefore what a minute of its calls costs. "
            "Every plan prices all three tiers, so this is a cost choice rather than "
            "an entitlement. The tier a call is actually billed at comes from the call "
            "log; this is the intent and the fallback."
        ),
    )
    timezone = models.ForeignKey(
        "onboard.Timezone",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="agents",
        help_text=(
            "The timezone this agent SPEAKS in. Times quoted to and heard from the "
            "caller are in this zone; everything crossing the API stays UTC. "
            "Blank falls back to the tenant's base timezone."
        ),
    )
    knowledge_bases = models.ManyToManyField(
        "core.KnowledgeBase",
        blank=True,
        related_name="agents",
        help_text="Knowledge bases associated directly with this agent.",
    )
    probing_questions = models.ManyToManyField(
        "core.ProbingQuestion",
        blank=True,
        related_name="agents",
        help_text="Probing questions associated directly with this agent.",
    )
    event_templates = models.ManyToManyField(
        "core.EventTemplate",
        blank=True,
        related_name="agents",
        help_text="Bookable meeting types this agent is allowed to schedule.",
    )
    transfer_destinations = models.JSONField(
        blank=True,
        null=True,
        help_text="Upcoming - Transfer configurations and destinations.",
    )

    class Meta:
        db_table = "core_agents"
        verbose_name = "Agent"
        verbose_name_plural = "Agents"
        constraints = [
            models.UniqueConstraint(
                fields=["bot_name", "tenant"],
                condition=models.Q(is_deleted=False),
                name="unique_active_agent_bot_name_tenant",
            )
        ]

    def soft_delete(self, user=None):
        """
        Custom soft delete implementation for Agent.
        Locks row and flags as deleted, renaming the bot_name to avoid uniqueness clashes.

        Refuses while a live campaign depends on this agent. PROTECT on the FK cannot
        cover this: agents are soft-deleted, so the database constraint never fires and
        the campaign would be left pointing at a deleted bot mid-run.
        """
        from django.core.exceptions import ValidationError
        from django.db import transaction
        from django.utils import timezone

        with transaction.atomic():
            # Lock row in DB
            Agent.objects.select_for_update().get(pk=self.pk)

            blocking = Campaign.objects.filter(
                agent=self, status__in=Campaign.LIVE_STATUSES
            ).count()
            if blocking:
                raise ValidationError(
                    f"This bot is used by {blocking} campaign(s) that are still "
                    "running. Cancel them before deleting the bot."
                )

            self.is_deleted = True
            self.deleted_at = timezone.now()
            if user:
                self.deleted_by = user

            # Append deleted timestamp to prevent unique constraint conflict on active bot names
            suffix = f"-deleted-{self.deleted_at.strftime('%Y%m%d%H%M%S')}"
            max_name_len = 100 - len(suffix)
            self.bot_name = f"{self.bot_name[:max_name_len]}{suffix}"

            self.save(
                update_fields=["is_deleted", "deleted_at", "bot_name", "deleted_by"]
                if user
                else ["is_deleted", "deleted_at", "bot_name"]
            )

    def __str__(self):
        return f"{self.bot_name} ({self.persona_name})"
