from django.core.validators import MinLengthValidator, MaxLengthValidator, RegexValidator
from django.db import models
from tenant.common.models.tenant_base_model import TenantBaseModel


class AgentTemplate(TenantBaseModel):
    """
    Model representing pre-defined or custom Voicebot Agent Templates.
    Fully multi-tenant and audited.
    """
    tenant = models.ForeignKey(
        "tenant.Tenant",
        on_delete=models.CASCADE,
        related_name="core_agenttemplate_tenant",
        null=True,
        blank=True,
        help_text="Tenant context partition key for row-level security isolation.",
    )
    name = models.CharField(
        max_length=100,
        validators=[
            MinLengthValidator(2),
            MaxLengthValidator(100),
            RegexValidator(
                regex=r"^[\w\s-]+$",
                message="Template Name can only contain letters, numbers, spaces, hyphens, and underscores.",
            ),
        ],
        help_text="Name of the template (e.g., 'Wealth Management Advisor').",
    )
    short_description = models.TextField(
        validators=[
            MinLengthValidator(10),
            MaxLengthValidator(300),
        ],
        help_text="Brief description of what the bot does and who it helps.",
    )
    visual_icon = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text="Predefined visual icon representing the template.",
    )
    primary_accent = models.CharField(
        max_length=7,
        blank=True,
        null=True,
        help_text="Accent color code (hex) for template cards and highlights.",
    )
    agent_prompt = models.TextField(
        validators=[
            MinLengthValidator(30),
            MaxLengthValidator(50000),
        ],
        help_text="Core instruction prompt defining personality, constraints, and behavior.",
    )
    strict_facts = models.BooleanField(
        default=True,
        help_text="When enabled, the agent avoids hallucinations and sticks strictly to facts.",
    )
    can_user_edit_flow = models.BooleanField(
        default=True,
        help_text="When enabled, the user can edit the agent's conversation flow.",
    )
    is_default = models.BooleanField(
        default=False,
        help_text="Determines whether this is a system-wide default agent template.",
    )
    class IndustryChoices(models.TextChoices):
        CLINIC_RECEPTIONIST = "CLINIC_RECEPTIONIST", "Clinic Receptionist"
        AGRICULTURAL_SUPPLIER = "AGRICULTURAL_SUPPLIER", "Agricultural Supplier"
        REAL_ESTATE = "REAL_ESTATE", "Real Estate"
        E_COMMERCE = "E_COMMERCE", "E-Commerce"
        RESTAURANT = "RESTAURANT", "Restaurant"
        GENERAL_ASSISTANT = "GENERAL_ASSISTANT", "General Assistant"
        OTHER = "OTHER", "Other"

    industry = models.CharField(
        max_length=50,
        choices=IndustryChoices.choices,
        default=IndustryChoices.GENERAL_ASSISTANT,
        help_text="Selected industry category for the template.",
    )
    probing_questions = models.ManyToManyField(
        "core.ProbingQuestion",
        blank=True,
        related_name="agent_templates",
        help_text="Probing questions associated with this template.",
    )

    class Meta:
        db_table = "core_agent_templates"
        verbose_name = "Agent Template"
        verbose_name_plural = "Agent Templates"
        constraints = [
            models.UniqueConstraint(
                fields=["name", "tenant"],
                condition=models.Q(is_deleted=False),
                name="unique_active_agent_template_tenant",
            )
        ]

    def soft_delete(self, user=None):
        """
        Custom soft delete implementation for AgentTemplate.
        Locks row and flags as deleted, and cascades soft delete to related probing questions.
        """
        from django.db import transaction
        from django.utils import timezone
        from django.core.exceptions import ValidationError

        if self.is_default:
            raise ValidationError("This is a default agent template and cannot be deleted.")

        with transaction.atomic():
            # Lock row in DB
            AgentTemplate.objects.select_for_update().get(pk=self.pk)
            
            self.is_deleted = True
            self.deleted_at = timezone.now()
            if user:
                self.deleted_by = user

            # Append deleted timestamp to prevent unique constraint conflict on active names
            suffix = f"-deleted-{self.deleted_at.strftime('%Y%m%d%H%M%S')}"
            max_name_len = 100 - len(suffix)
            self.name = f"{self.name[:max_name_len]}{suffix}"

            self.save(
                update_fields=["is_deleted", "deleted_at", "name", "deleted_by"]
                if user
                else ["is_deleted", "deleted_at", "name"]
            )

    def __str__(self):
        return self.name
