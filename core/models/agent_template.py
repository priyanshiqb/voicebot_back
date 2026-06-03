from django.core.validators import MinLengthValidator, MaxLengthValidator, RegexValidator
from django.db import models
from tenant.common.models.tenant_base_model import TenantBaseModel


class AgentTemplate(TenantBaseModel):
    """
    Model representing pre-defined or custom Voicebot Agent Templates.
    Fully multi-tenant and audited.
    """
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
            MaxLengthValidator(5000),
        ],
        help_text="Core instruction prompt defining personality, constraints, and behavior.",
    )
    strict_facts = models.BooleanField(
        default=True,
        help_text="When enabled, the agent avoids hallucinations and sticks strictly to facts.",
    )
    can_user_edit_flow = models.BooleanField(
        default=False,
        help_text="When enabled, the user can edit the agent's conversation flow.",
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

        with transaction.atomic():
            template = AgentTemplate.objects.select_for_update().get(pk=self.pk)
            template.is_deleted = True
            template.deleted_at = timezone.now()
            if user:
                template.deleted_by = user

            # Append deleted timestamp to prevent unique constraint conflict on active names
            suffix = f"-deleted-{template.deleted_at.strftime('%Y%m%d%H%M%S')}"
            template.name = f"{template.name}{suffix}"

            template.save(
                update_fields=["is_deleted", "deleted_at", "name", "deleted_by"]
                if user
                else ["is_deleted", "deleted_at", "name"]
            )

            # Cascade soft-delete to linked probing questions
            for question in template.probing_questions.filter(is_deleted=False):
                question.soft_delete(user=user)

    def __str__(self):
        return self.name
