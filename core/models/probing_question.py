from django.core.validators import (
    MinLengthValidator,
    MaxLengthValidator,
    MinValueValidator,
    MaxValueValidator,
)
from django.db import models
from tenant.common.models.tenant_base_model import TenantBaseModel


class ProbingQuestion(TenantBaseModel):
    """
    Model representing manual probing questions belonging to a specific AgentTemplate.
    Fully multi-tenant, audited, and soft-delete compatible.
    """
    agent_template = models.ForeignKey(
        "core.AgentTemplate",
        on_delete=models.CASCADE,
        related_name="probing_questions",
        help_text="The agent template that this probing question belongs to.",
    )
    question = models.CharField(
        max_length=100,
        validators=[
            MinLengthValidator(10),
            MaxLengthValidator(100),
        ],
        help_text="The probing question text to be added.",
    )
    priority = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        help_text="Display and execution priority relative to others.",
    )
    score = models.PositiveIntegerField(
        validators=[
            MinValueValidator(0),
            MaxValueValidator(100),
        ],
        help_text="Weight/score assigned to this question (0-100).",
    )
    is_mandatory = models.BooleanField(
        default=False,
        help_text="Determines whether the agent must ask this question in every interaction.",
    )

    class Meta:
        db_table = "core_probing_questions"
        verbose_name = "Probing Question"
        verbose_name_plural = "Probing Questions"
        constraints = [
            models.UniqueConstraint(
                fields=["question", "agent_template"],
                condition=models.Q(is_deleted=False),
                name="unique_active_probing_question_agent_template",
            )
        ]

    def soft_delete(self, user=None):
        """
        Custom soft delete implementation for ProbingQuestion.
        Locks row and flags as deleted.
        """
        from django.db import transaction
        from django.utils import timezone

        with transaction.atomic():
            question = ProbingQuestion.objects.select_for_update().get(pk=self.pk)
            question.is_deleted = True
            question.deleted_at = timezone.now()
            if user:
                question.deleted_by = user

            # Append deleted timestamp to prevent unique constraint conflicts on active question texts
            suffix = f"-deleted-{question.deleted_at.strftime('%Y%m%d%H%M%S')}"
            question.question = f"{question.question}{suffix}"

            question.save(
                update_fields=["is_deleted", "deleted_at", "question", "deleted_by"]
                if user
                else ["is_deleted", "deleted_at", "question"]
            )

    def __str__(self):
        return f"{self.question} (Priority: {self.priority})"
