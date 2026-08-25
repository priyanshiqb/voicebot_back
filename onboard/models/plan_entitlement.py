from decimal import Decimal

from django.db import models

from onboard.constants.entitlement_resource import EntitlementResource
from onboard.constants.reset_period import ResetPeriod
from utils.models.base_model_core import BaseModelCore


class PlanEntitlement(BaseModelCore):
    """
    What one plan includes of one resource, and what more of it costs.

    The **entitlement catalogue** — one row per (plan, resource). This replaced a set
    of ``max_*`` columns on ``Plan``: once every resource needs an included amount, an
    availability flag, a price and a ceiling, columns become roughly four per resource
    and every new resource is a migration. As rows, a new resource is a row.

    Deliberately the mirror image of ``wallet.CreditRate``, which does the same job for
    per-minute credit prices. The difference is what they measure:

    * ``CreditRate`` prices a **flow** — minutes consumed, gone once spent, in credits.
    * this prices a **gauge** — seats held, freed again when deleted, in rupees.

    Because add-ons are recurring rupees, buying one changes the tenant's subscription
    price. That makes this the billing engine's concern, not the wallet's: no row here
    ever produces a ledger entry.

    **Not versioned**, unlike ``CreditRate``. It does not need to be — the included
    amount is frozen into ``Subscription.plan_snapshot`` at purchase, and the add-on
    price is frozen onto ``TenantEntitlement`` when extras are bought. Editing a row
    changes what *new and renewing* subscribers get, which is the same rule
    ``Plan.allocated_credits`` follows. Per-minute rates earn their version history
    because they change often and every historical charge must stay reproducible;
    entitlement config changes rarely and is already snapshotted twice.
    """

    plan = models.ForeignKey(
        "onboard.Plan",
        on_delete=models.CASCADE,
        related_name="entitlements",
        help_text="The plan this allowance belongs to.",
    )
    resource = models.CharField(
        max_length=32,
        choices=EntitlementResource.choices,
        db_index=True,
        help_text="Which countable resource this row governs.",
    )
    included_quantity = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text=(
            "How many the plan includes at no extra cost. Null means unlimited, in "
            "which case add-ons must be off — you cannot sell more of something a "
            "tenant already has without limit."
        ),
    )

    reset_period = models.CharField(
        max_length=16,
        choices=ResetPeriod.choices,
        default=ResetPeriod.NONE,
        help_text=(
            "How often this allowance refills. NONE is a standing total: three "
            "agents means three agents, and deleting one gives the seat back — "
            "right for anything a tenant *holds*. Any other value makes it "
            "windowed, meaning what is limited is the *act* rather than the row, "
            "so it is counted from when the act happened and deleting the row "
            "does not give the allowance back. Starting a campaign is the "
            "example — the calls went out whether or not the campaign still "
            "exists. Windows are anchored to the tenant's own subscription "
            "rather than the calendar, so each one sits inside a billing cycle "
            "instead of straddling one."
        ),
    )

    # --- add-on configuration -------------------------------------------------
    is_addon_available = models.BooleanField(
        default=False,
        help_text=(
            "Whether the tenant may buy beyond the included amount. False makes the "
            "included amount a hard ceiling, and upgrading the plan is the only way "
            "past it."
        ),
    )
    price_per_additional = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text=(
            "Pre-tax rupees per additional block, charged every billing cycle for as "
            "long as the tenant keeps the extra. Tax is applied at the plan's rate, "
            "the same as the plan's own price."
        ),
    )
    addon_step = models.PositiveIntegerField(
        default=1,
        help_text=(
            "Block size an add-on is sold in. 1 for seats — one agent at a time. "
            "Larger for granular resources: storage sold in 10 GB blocks means "
            "step 10, and price_per_additional is the price of the whole block."
        ),
    )
    max_additional = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text=(
            "Ceiling on how much extra may be bought, in units (not blocks). Null "
            "means uncapped. A limit here is what stops a tenant assembling an "
            "enterprise footprint on the cheapest plan's add-on pricing."
        ),
    )

    class Meta:
        db_table = "onboard_plan_entitlement"
        verbose_name = "Plan Entitlement"
        verbose_name_plural = "Plan Entitlements"
        ordering = ["plan", "resource"]
        constraints = [
            models.UniqueConstraint(
                fields=["plan", "resource"],
                condition=models.Q(is_deleted=False),
                name="uniq_entitlement_per_plan_resource",
            ),
            # Unlimited plus purchasable is incoherent, and it would make the
            # effective-limit calculation ambiguous rather than merely wrong.
            models.CheckConstraint(
                condition=models.Q(included_quantity__isnull=False)
                | models.Q(is_addon_available=False),
                name="ck_entitlement_no_addon_when_unlimited",
            ),
            models.CheckConstraint(
                condition=models.Q(addon_step__gte=1),
                name="ck_entitlement_addon_step_positive",
            ),
        ]

    @property
    def is_unlimited(self):
        return self.included_quantity is None

    def price_for(self, additional_units):
        """Recurring pre-tax rupees for ``additional_units`` extra of this resource.

        Charged per *block*, rounded up: on a 10 GB step, asking for 12 GB bills two
        blocks. Billing a fraction of a block would mean either giving away the
        remainder or invoicing a number the tenant cannot reconcile against what they
        asked for.
        """
        if additional_units <= 0:
            return Decimal("0.00")
        blocks = -(-additional_units // self.addon_step)  # ceiling division
        return (self.price_per_additional * blocks).quantize(Decimal("0.01"))

    def __str__(self):
        included = "unlimited" if self.is_unlimited else self.included_quantity
        addon = f", +{self.addon_step} @ {self.price_per_additional}" if self.is_addon_available else ""
        return f"{self.get_resource_display()}: {included}{addon}"
