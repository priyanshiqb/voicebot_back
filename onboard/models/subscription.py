from django.db import models
from utils.models.base_model_core import BaseModelCore
from django.utils import timezone


class Subscription(BaseModelCore):
    """
    Subscription Model representing an active or historical membership plan
    assigned to a Tenant organization.
    """
    class SubscriptionStatusChoices(models.TextChoices):
        ACTIVE = "active", "Active"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"
        TRIALING = "trialing", "Trialing"

    tenant = models.ForeignKey(
        "tenant.Tenant",
        on_delete=models.CASCADE,
        related_name="subscriptions",
        help_text="Reference to the Tenant organization."
    )
    plan = models.ForeignKey(
        "onboard.Plan",
        on_delete=models.PROTECT,
        related_name="subscriptions",
        help_text="The Plan model the tenant is subscribed to."
    )
    status = models.CharField(
        max_length=20,
        choices=SubscriptionStatusChoices.choices,
        default=SubscriptionStatusChoices.ACTIVE,
        help_text="Current state of the subscription."
    )
    start_date = models.DateTimeField(
        default=timezone.now,
        help_text="Start date and time of the subscription period."
    )
    end_date = models.DateTimeField(
        blank=True,
        null=True,
        help_text="End date and time of the subscription period."
    )
    plan_snapshot = models.JSONField(
        blank=True,
        null=True,
        help_text="A frozen JSON snapshot of the Plan pricing and details at the moment of subscription."
    )

    class Meta:
        db_table = "onboard_subscription"
        ordering = ["-created_at"]

    def generate_plan_snapshot(self):
        """Generates and saves the frozen snapshot of the plan."""
        if self.plan:
            self.plan_snapshot = {
                "id": str(self.plan.id),
                "code": self.plan.code,
                "name": self.plan.name,
                "description": self.plan.description,
                "base_price": str(self.plan.base_price),
                "discount_percentage": str(self.plan.discount_percentage),
                "discount_amount": str(self.plan.discount_amount),
                "net_base_price": str(self.plan.net_base_price),
                "tax_percentage": str(self.plan.tax_percentage),
                "tax_amount": str(self.plan.tax_amount),
                "total_price": str(self.plan.total_price),
                "currency": self.plan.currency,
                "billing_cycle": self.plan.billing_cycle,
                "plan_type": self.plan.plan_type,
            }

    def save(self, *args, **kwargs):
        if not self.plan_snapshot and self.plan:
            self.generate_plan_snapshot()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.tenant.name} - {self.plan.name} ({self.status})"
