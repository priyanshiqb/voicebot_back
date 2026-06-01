from django.db import models
from utils.models.base_model_core import BaseModelCore
from decimal import Decimal, ROUND_HALF_UP


class Plan(BaseModelCore):
    """
    Plan Model representing user subscription/onboarding plans.
    Inherits from BaseModelCore for UUID primary keys, soft deletion, and timestamps.
    """

    class BillingCycleChoices(models.TextChoices):
        """
        Nested text choices for plan billing cycle.
        """
        MONTHLY = "monthly", "Monthly"
        YEARLY = "yearly", "Yearly"

    class PlanTypeChoices(models.TextChoices):
        """
        Nested text choices for plan type.
        """
        PAID = "paid", "Paid"
        FREE = "free", "Free"
        PAY_AS_YOU_GO = "pay_as_you_go", "Pay As You Go"

    class CurrencyChoices(models.TextChoices):
        """
        Nested text choices for currencies (limited to INR).
        """
        INR = "INR", "Indian Rupee"

    code = models.CharField(
        max_length=50,
        unique=True,
        help_text="Unique machine-readable code identifier for the plan."
    )
    name = models.CharField(
        max_length=100,
        unique=True,
        help_text="Unique name identifier for the subscription plan."
    )
    description = models.TextField(
        blank=True,
        null=True,
        help_text="Detailed description of features included in this plan."
    )
    base_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Original gross base price of the plan before discount and tax."
    )
    discount_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Discount percentage rate (e.g., 10.00 for 10% off)."
    )
    tax_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Tax percentage rate (e.g., 18.00 for 18%)."
    )
    currency = models.CharField(
        max_length=3,
        choices=CurrencyChoices.choices,
        default=CurrencyChoices.INR,
        help_text="Three-letter ISO currency code."
    )
    billing_cycle = models.CharField(
        max_length=20,
        choices=BillingCycleChoices.choices,
        default=BillingCycleChoices.MONTHLY,
        help_text="Billing frequency cycle."
    )
    plan_type = models.CharField(
        max_length=20,
        choices=PlanTypeChoices.choices,
        default=PlanTypeChoices.FREE,
        help_text="The classification type of the plan."
    )
    sort_order = models.PositiveIntegerField(
        default=0,
        help_text="Sort display order for the plan."
    )
    is_most_popular = models.BooleanField(
        default=False,
        help_text="Flag indicating whether this plan is marked as the most popular choice."
    )

    class Meta(BaseModelCore.Meta):
        verbose_name = "Plan"
        verbose_name_plural = "Plans"
        ordering = ["sort_order", "-created_at"]

    @property
    def discount_amount(self):
        """Calculates the absolute discount value dynamically."""
        base_price = Decimal(str(self.base_price or 0.0))
        discount_percentage = Decimal(str(self.discount_percentage or 0.0))
        val = (base_price * discount_percentage) / Decimal("100.00")
        return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def net_base_price(self):
        """Calculates the net price after discount (before tax)."""
        base_price = Decimal(str(self.base_price or 0.0))
        val = base_price - self.discount_amount
        return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def tax_amount(self):
        """Calculates tax on the discounted net base price."""
        net_base_price = self.net_base_price
        tax_percentage = Decimal(str(self.tax_percentage or 0.0))
        val = (net_base_price * tax_percentage) / Decimal("100.00")
        return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def total_price(self):
        """Calculates the final absolute total price (net base + tax)."""
        val = self.net_base_price + self.tax_amount
        return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def __str__(self):
        return self.name
