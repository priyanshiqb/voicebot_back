from django.db import models
from utils.models.base_model_core import BaseModelCore
from decimal import Decimal, ROUND_HALF_UP


class Plan(BaseModelCore):
    """
    Plan Model representing user subscription/onboarding plans.
    Inherits from BaseModelCore for UUID primary keys, soft deletion, and timestamps.
    """

    class Status(models.TextChoices):
        """Where a plan is in its life, which ``is_active`` alone could not say.

        ``is_active = False`` means both "draft, never sold" and "retired, with 47
        subscribers" -- indistinguishable, and the difference is exactly what somebody
        needs to know before clicking either button.

        Every transition is allowed in both directions. **Nothing is ever deleted**: a
        draft nobody finished is retired like anything else, and RETIRED reaches every
        state a delete would without leaving subscriptions and audit rows pointing at
        a row that is gone.
        """

        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        RETIRED = "retired", "Retired"

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
        # No plan currently uses this. Retained deliberately: pay-as-you-go is
        # expressible entirely as field values — allocated_credits=0, top-ups
        # enabled, allow_use_after_expiry=True, renews_without_payment=True — so
        # reinstating it is a data change. Its configuration is preserved in
        # json_files/plan_pay_as_you_go.json. Nothing branches on this member, and
        # nothing should: see renews_without_payment below.
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

    # ------------------------------------------------------------------ #
    # Credits
    #
    # What a cycle grants and what happens to leftovers. Per-operation
    # *prices* are not here — they are CreditRate rows pointing at this
    # plan, because prices need version history and these do not: they are
    # frozen into Subscription.plan_snapshot when a tenant subscribes.
    # ------------------------------------------------------------------ #
    allocated_credits = models.PositiveIntegerField(
        default=0,
        help_text="Credits granted at the start of every billing cycle on this plan."
    )
    cost_per_additional_credit = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text=(
            "Pre-tax price of one credit when topping up mid-cycle. "
            "0.00 disables top-ups on this plan."
        )
    )
    max_rollover_credits = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text=(
            "Ceiling on credits carried into the next cycle. Null means uncapped. "
            "Without a cap, a light user on an annual plan can bank a year of credits "
            "and then run them down while paying nothing."
        )
    )
    allow_rollover_on_renewal = models.BooleanField(
        default=False,
        help_text="Do unused plan credits survive a same-plan renewal?"
    )
    allow_rollover_on_upgrade = models.BooleanField(
        default=True,
        help_text="Do unused plan credits carry over when moving to a higher plan?"
    )
    allow_rollover_on_downgrade = models.BooleanField(
        default=False,
        help_text="Do unused plan credits carry over when moving to a lower plan?"
    )
    allow_use_after_expiry = models.BooleanField(
        default=False,
        help_text=(
            "May the tenant keep spending a remaining balance once the subscription "
            "has lapsed? Required for pay-as-you-go, where purchased credits must "
            "stay spendable."
        )
    )
    low_balance_threshold = models.PositiveIntegerField(
        default=0,
        help_text="Alert the tenant at or below this balance. 0 disables the alert."
    )
    renews_without_payment = models.BooleanField(
        default=False,
        help_text=(
            "Whether the cycle rolls over on its own, with no order and no payment. "
            "False means the subscription expires at end_date and access stops until "
            "the tenant pays.\n\n"
            "This is what separates the two kinds of zero-cost plan, which is "
            "otherwise impossible to tell apart from the price alone: a free trial "
            "costs nothing and is *meant* to end, while pay-as-you-go costs nothing "
            "up front and must never end, because the tenant may be holding credits "
            "they paid cash for. Keeping it a field rather than a branch on plan_type "
            "means adding pay-as-you-go back is data, not code."
        )
    )

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
        help_text=(
            "Draft until deliberately activated. A plan is three models and 84 "
            "settings; one created with only the first is silently permissive -- a "
            "missing entitlement row reads as unlimited and a missing rate cannot "
            "price a call. Starting as a draft means a plan has to be completed "
            "before it can be sold, rather than sold before it is completed."
        ),
    )

    class Meta(BaseModelCore.Meta):
        verbose_name = "Plan"
        verbose_name_plural = "Plans"
        ordering = ["sort_order", "-created_at"]
        constraints = [
            # A priced plan that renews itself for free would silently stop billing.
            # base_price rather than total_price because the latter is a property and
            # cannot be constrained; a plan with a zero base is zero-cost whatever
            # the discount and tax do to it.
            models.CheckConstraint(
                condition=models.Q(renews_without_payment=False)
                | models.Q(base_price=Decimal("0.00")),
                name="ck_plan_free_renewal_only_when_zero_cost",
            ),
        ]

    # --- status is authoritative; is_active follows it ------------------------
    #
    # `is_active` comes from BaseModelCore, cannot be removed, and is filtered in six
    # production paths -- signup, eligible-plans, preview-change, change-plan,
    # update-plan and the readiness audit. Rather than change all six, `status` drives
    # it here so every one of them keeps working untouched.
    #
    # Two fields for one fact is normally how they drift. The derivation is what stops
    # it, and it belongs here rather than in each caller.

    def save(self, *args, **kwargs):
        self.is_active = self.status == self.Status.ACTIVE
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "status" in update_fields:
            # A partial save that moves the status has to carry the derived column
            # with it, or the row is written half-updated.
            kwargs["update_fields"] = list(update_fields) + ["is_active"]
        super().save(*args, **kwargs)

    def activate(self):
        """Mechanical. **The completeness gate lives in the service, not here.**

        Overridden because ``BaseModelCore.activate()`` writes ``is_active`` alone,
        which ``save()`` above would immediately recompute from an unchanged status --
        silently doing nothing. Anything reaching for this method wants the status to
        move, so it moves.
        """
        self.status = self.Status.ACTIVE
        self.save(update_fields=["status", "updated_at"])

    def deactivate(self):
        """Retire the plan. Stops new subscriptions; existing ones are untouched.

        Same reason for the override as :meth:`activate` -- and this one matters more,
        because a ``deactivate()`` that silently left the plan active would keep
        selling something somebody believed they had withdrawn.
        """
        self.status = self.Status.RETIRED
        self.save(update_fields=["status", "updated_at"])

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
