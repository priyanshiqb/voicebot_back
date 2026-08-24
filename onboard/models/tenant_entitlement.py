from decimal import Decimal

from django.db import models

from onboard.constants.entitlement_resource import EntitlementResource
from tenant.common.models.tenant_base_model_core import TenantBaseModelCore


class TenantEntitlement(TenantBaseModelCore):
    """
    Extra capacity a tenant has bought beyond what their plan includes.

    One row per (tenant, resource), holding a quantity and the price it is re-charged
    at. The tenant's real limit is::

        plan_snapshot included  +  this row's additional_quantity

    with the included half frozen at subscription time and the extra half here.

    **One row per resource, not one per purchase.** Buying more re-prices the whole
    block to the current price, which is how seat-based billing works everywhere:
    "you have 5 extra agents at ₹299 each", not five rows at four historical prices.
    An invoice line nobody can reconcile against a seat count is worse than losing the
    purchase history — and the purchase history lives in ``Transaction`` regardless.

    **These are gauges, so nothing here touches the wallet.** Add-ons are recurring
    rupees on the subscription; they never produce a credit ledger entry. A tenant who
    runs out of credits keeps their seats, and a tenant who deletes an agent gets the
    allowance back without a refund question.

    **Extras survive renewals and plan changes**, and are re-charged each cycle. When a
    plan change means the new plan already includes what they were paying extra for,
    ``additional_quantity`` is reduced by that much so nobody pays twice for the same
    seat — and it is reduced, never silently zeroed, because anything above the new
    plan's included amount is still legitimately theirs.
    """

    resource = models.CharField(
        max_length=32,
        choices=EntitlementResource.choices,
        db_index=True,
        help_text="Which countable resource this extra capacity is for.",
    )
    additional_quantity = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Units bought beyond the plan's included amount. Zero is a legitimate "
            "resting state — the row survives so the price and history stay attached "
            "if they buy again."
        ),
    )

    # --- frozen at purchase ---------------------------------------------------
    # A price rise must not silently re-price capacity a tenant already holds; they
    # keep what they agreed to until they change something. Same rule the plan's own
    # price follows via plan_snapshot.
    price_per_unit = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text=(
            "Pre-tax rupees per block, frozen when the extras were last bought. This "
            "is what the recurring charge is calculated from, not the plan's current "
            "catalogue price."
        ),
    )
    addon_step = models.PositiveIntegerField(
        default=1,
        help_text="Block size at the time of purchase, frozen alongside the price.",
    )

    # --- provenance -----------------------------------------------------------
    subscription = models.ForeignKey(
        "onboard.Subscription",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="entitlements",
        help_text=(
            "The subscription live when these extras were last bought. Context only — "
            "extras outlive the subscription that bought them."
        ),
    )
    purchased_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="When the quantity was last changed by a purchase.",
    )
    notes = models.TextField(
        blank=True,
        null=True,
        help_text="Why this capacity exists — a support grant, a negotiated deal.",
    )

    class Meta:
        db_table = "onboard_tenant_entitlement"
        verbose_name = "Tenant Entitlement"
        verbose_name_plural = "Tenant Entitlements"
        ordering = ["tenant", "resource"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "resource"],
                condition=models.Q(is_deleted=False),
                name="uniq_tenant_entitlement_per_resource",
            ),
            models.CheckConstraint(
                condition=models.Q(addon_step__gte=1),
                name="ck_tenant_entitlement_addon_step_positive",
            ),
        ]

    @property
    def recurring_charge(self):
        """Pre-tax rupees this row adds to every billing cycle.

        Priced per block and rounded up, matching ``PlanEntitlement.price_for`` — the
        quote a tenant is shown and the amount they are charged have to come out of the
        same arithmetic.
        """
        if self.additional_quantity <= 0:
            return Decimal("0.00")
        blocks = -(-self.additional_quantity // self.addon_step)
        return (self.price_per_unit * blocks).quantize(Decimal("0.01"))

    def __str__(self):
        return f"{self.tenant} +{self.additional_quantity} {self.get_resource_display()}"
