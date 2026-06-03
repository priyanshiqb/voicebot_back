from django.core.validators import MinLengthValidator, MaxLengthValidator, RegexValidator
from django.db import models
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

    class IndustryChoices(models.TextChoices):
        CLINIC_RECEPTIONIST = "CLINIC_RECEPTIONIST", "Clinic Receptionist"
        AGRICULTURAL_SUPPLIER = "AGRICULTURAL_SUPPLIER", "Agricultural Supplier"
        REAL_ESTATE = "REAL_ESTATE", "Real Estate"
        E_COMMERCE = "E_COMMERCE", "E-Commerce"
        RESTAURANT = "RESTAURANT", "Restaurant"
        GENERAL_ASSISTANT = "GENERAL_ASSISTANT", "General Assistant"
        OTHER = "OTHER", "Other"

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
    industry = models.CharField(
        max_length=50,
        choices=IndustryChoices.choices,
        default=IndustryChoices.GENERAL_ASSISTANT,
        help_text="Selected industry category for the agent.",
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
    knowledge_bases = models.ManyToManyField(
        "core.KnowledgeBase",
        blank=True,
        related_name="agents",
        help_text="Knowledge bases associated directly with this agent.",
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
        """
        from django.db import transaction
        from django.utils import timezone

        with transaction.atomic():
            agent = Agent.objects.select_for_update().get(pk=self.pk)
            agent.is_deleted = True
            agent.deleted_at = timezone.now()
            if user:
                agent.deleted_by = user

            # Append deleted timestamp to prevent unique constraint conflict on active bot names
            suffix = f"-deleted-{agent.deleted_at.strftime('%Y%m%d%H%M%S')}"
            agent.bot_name = f"{agent.bot_name}{suffix}"

            agent.save(
                update_fields=["is_deleted", "deleted_at", "bot_name", "deleted_by"]
                if user
                else ["is_deleted", "deleted_at", "bot_name"]
            )

    def __str__(self):
        return f"{self.bot_name} ({self.persona_name})"
