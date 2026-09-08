from dateutil.relativedelta import relativedelta
from django.db import models
from django.utils import timezone

from onboard.models.plan import Plan
from utils.models.base_model_core import BaseModelCore


class Subscription(BaseModelCore):
    """
    Subscription Model representing an active or historical membership plan
    assigned to a Tenant organization.

    **The contract, not the balance.** What the tenant bought and until when. Credits
    live in ``wallet.Wallet``, which outlives this row: a plan change closes one
    subscription and opens another, and the wallet never moves. That is why there is
    no ``credit_balance`` field here.

    **The terms are frozen at purchase** into ``plan_snapshot`` — price, credit rules
    and entitlement allowances. Everything downstream reads the snapshot rather than
    the live plan, so editing a plan changes terms for *future* cycles only. Read them
    through the ``rules`` property, never off ``self.plan`` directly.
    """

    class SubscriptionStatusChoices(models.TextChoices):
        ACTIVE = "active", "Active"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"
        TRIALING = "trialing", "Trialing"

    #: One billing cycle, as calendar arithmetic. Not 30 days: a cycle starting
    #: 31 January must renew on 28 February, and twelve monthly cycles must land on
    #: the same calendar day a year later rather than drifting five days earlier.
    CYCLE_DELTAS = {
        Plan.BillingCycleChoices.MONTHLY: relativedelta(months=1),
        Plan.BillingCycleChoices.YEARLY: relativedelta(years=1),
    }

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
        help_text=(
            "When this billing cycle ends. Derived from start_date plus the plan's "
            "billing cycle when not set explicitly. "
            "Non-nullable, deliberately: expiry, renewal windows and proration are all "
            "functions of this, so a subscription without one has no billing cycle to "
            "reason about — and every row in this table was in exactly that state "
            "until it was backfilled. save() fills it when unset, so the database "
            "constraint is a backstop rather than a burden on callers."
        )
    )
    plan_snapshot = models.JSONField(
        blank=True,
        null=True,
        help_text=(
            "Frozen copy of the plan's pricing, credit rules and entitlement "
            "allowances at the moment of subscription. The contract with the tenant."
        )
    )

    # --- lifecycle ------------------------------------------------------------
    canceled_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="When this subscription was voided by an upgrade, downgrade or churn."
    )
    superseded_by = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="supersedes",
        help_text=(
            "The subscription that replaced this one. Makes the chain navigable, "
            "which is the first thing support asks for when a tenant disputes a "
            "rollover."
        )
    )

    # --- what was bought ------------------------------------------------------
    selected_minutes = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text=(
            "Minutes this subscription committed to, on a minutes-based plan. The "
            "quantity every number below is derived from, and the one a renewal "
            "repeats.\n\n"
            "Null on a legacy subscription, and deliberately never backfilled -- "
            "deriving it from a fixed allocation would invent a commitment nobody "
            "made."
        ),
    )
    allocated_credits = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Credits this cycle granted. **Moved here from Plan**, because the grant "
            "is a fact about a sale rather than about a catalogue row: once it "
            "depends on a quantity, one number on the plan cannot answer it. The "
            "plan answers the other question -- what N minutes *would* grant -- "
            "through credits_for(N).\n\n"
            "Written from the same value passed to wallet.grant(), so this column "
            "and the credit ledger cannot disagree about what a cycle was worth. "
            "That includes a negotiated credit_grant override, which appears nowhere "
            "else.\n\n"
            "Non-null: Plan.allocated_credits is gone, so this is now the only "
            "record of what a cycle was worth, and a null would be a subscription "
            "that cannot say what it was sold."
        ),
    )

    # --- a change waiting for the cycle to end --------------------------------
    # A downgrade is stored, never applied early: the tenant already paid for the
    # cycle they are in, and ending it would mean refunding cash or taking away
    # something they paid for. Storing it also means a second subscription is never
    # created ahead of time, so the one-active-per-tenant constraint below is never
    # even approached.
    scheduled_plan = models.ForeignKey(
        "onboard.Plan",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="scheduled_subscriptions",
        help_text="A plan change waiting to take effect at the end of this cycle."
    )
    scheduled_change_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="When the scheduled change fires. Normally equal to end_date."
    )
    scheduled_minutes = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text=(
            "The quantity the scheduled change moves to.\n\n"
            "**Needed because a downgrade need not change plan.** Dropping from 2,000 "
            "minutes to 600 on the same plan is a downgrade like any other, and "
            "``scheduled_plan`` alone cannot express it -- it would hold the plan the "
            "tenant is already on, and the change would fire at cycle end having "
            "moved nothing.\n\n"
            "Null means the scheduled change keeps the current quantity, which is "
            "what an ordinary plan-only downgrade wants."
        ),
    )

    class Meta:
        db_table = "onboard_subscription"
        ordering = ["-created_at"]
        constraints = [
            # Exactly one live subscription per tenant. Without this, an upgrade that
            # creates a second row makes "the current subscription" ambiguous, and
            # the wrong one can be read. `is_deleted=False` so a soft-deleted row
            # never blocks a legitimate replacement.
            models.UniqueConstraint(
                fields=["tenant"],
                condition=models.Q(status="active", is_deleted=False),
                name="uniq_active_subscription_per_tenant",
            ),
        ]

    # --- cycle arithmetic -----------------------------------------------------

    def cycle_delta(self):
        """One billing cycle for this subscription's plan.

        Falls back to monthly for a plan whose cycle is unrecognised — a subscription
        with no cycle at all is worse than one with a conservative guess, because
        expiry silently never fires for it.
        """
        cycle = (self.plan_snapshot or {}).get("billing_cycle")
        if not cycle and self.plan_id:
            cycle = self.plan.billing_cycle
        return self.CYCLE_DELTAS.get(cycle, self.CYCLE_DELTAS[Plan.BillingCycleChoices.MONTHLY])

    def derive_end_date(self):
        """``start_date`` plus one billing cycle."""
        return (self.start_date or timezone.now()) + self.cycle_delta()

    # --- the frozen contract --------------------------------------------------

    def generate_plan_snapshot(self):
        """Freeze the plan's pricing, credit rules and allowances onto this row.

        **The price is frozen for the quantity that was bought**, not for the
        catalogue. On a minutes-based plan the price keys below hold
        ``selected_minutes x price_per_minute_inr``, worked through discount and tax;
        on a legacy plan they hold exactly what they always did.

        Writing the computed figures into the *existing* keys is what keeps the rest
        of billing untouched. ``pricing.quote_plan_change()`` values a tenant's unused
        time from ``net_base_price``, and ``rules`` exposes ``allocated_credits`` --
        both now mean "what this customer actually pays and gets", which is what those
        readers wanted all along.
        """
        if not self.plan:
            return

        price = self.plan.price_components(self.selected_minutes)
        basis = self.plan.basis_rate() if self.plan.credit_basis else None

        self.plan_snapshot = {
            "id": str(self.plan.id),
            "code": self.plan.code,
            "name": self.plan.name,
            "description": self.plan.description,
            "base_price": str(price.gross),
            "discount_percentage": str(self.plan.discount_percentage),
            "discount_amount": str(price.discount),
            "net_base_price": str(price.net),
            "tax_percentage": str(self.plan.tax_percentage),
            "tax_amount": str(price.tax),
            "total_price": str(price.total),
            "currency": self.plan.currency,
            "billing_cycle": self.plan.billing_cycle,
            "plan_type": self.plan.plan_type,
            # What was bought, and the two per-minute figures it was priced and
            # granted at. Frozen rather than looked up later: the basis rate can be
            # superseded the day after a tenant subscribes, and their cycle must not
            # silently re-price or re-grant. Renewal is where a new rate legitimately
            # reaches them, and renewal re-freezes.
            "selected_minutes": self.selected_minutes,
            "credit_basis": self.plan.credit_basis,
            "price_per_minute_inr": (
                str(basis.price_per_minute_inr)
                if basis is not None and basis.price_per_minute_inr is not None
                else None
            ),
            "credits_per_minute": self.plan.credits_per_minute(),
            # Credit rules. Without these frozen, an admin toggling a rollover flag
            # silently rewrites the terms of every live subscription.
            #
            # `allocated_credits` keeps its name and its meaning -- "what this cycle
            # grants" -- and only its source moves, from the plan to this row. Every
            # existing reader of rules["allocated_credits"] stays correct untouched.
            # The column when it says something, the plan's derivation otherwise. A
            # row built without going through `activate()` has the field default of
            # zero, and freezing that would record a cycle as worth nothing.
            "allocated_credits": (
                self.allocated_credits
                or self.plan.credits_for(self.selected_minutes)
            ),
            "cost_per_additional_credit": str(self.plan.cost_per_additional_credit),
            "max_rollover_credits": self.plan.max_rollover_credits,
            "max_rollover_ratio": (
                str(self.plan.max_rollover_ratio)
                if self.plan.max_rollover_ratio is not None else None
            ),
            "low_balance_percent": self.plan.low_balance_percent,
            "allow_rollover_on_renewal": self.plan.allow_rollover_on_renewal,
            "allow_rollover_on_upgrade": self.plan.allow_rollover_on_upgrade,
            "allow_rollover_on_downgrade": self.plan.allow_rollover_on_downgrade,
            "allow_use_after_expiry": self.plan.allow_use_after_expiry,
            "low_balance_threshold": self.plan.low_balance_threshold,
            "renews_without_payment": self.plan.renews_without_payment,
            # Included allowances, read by the entitlement checker. Purchased extras
            # live on TenantEntitlement; the effective limit is the sum.
            #
            # The *window* is frozen beside the number, deliberately. An allowance of
            # "10" means nothing without knowing whether that is ten a month or ten a
            # year — and if the period lived only in code, changing it would silently
            # re-interpret every subscription already sold.
            "entitlements": {
                e.resource: {
                    "included": e.included_quantity,
                    "reset_period": e.reset_period,
                }
                for e in self.plan.entitlements.all()
            },
        }

    @property
    def rules(self):
        """The credit rules in force for this cycle.

        Snapshot first, live plan as a fallback for any key the snapshot predates.
        Every caller must read rules through here rather than off ``self.plan``, or
        the freeze guarantee is only as good as the discipline of the last person to
        touch the calling code.
        """
        snapshot = self.plan_snapshot or {}
        keys = (
            "allocated_credits",
            "cost_per_additional_credit",
            "max_rollover_credits",
            "allow_rollover_on_renewal",
            "allow_rollover_on_upgrade",
            "allow_rollover_on_downgrade",
            "allow_use_after_expiry",
            "low_balance_threshold",
            "low_balance_percent",
            "max_rollover_ratio",
            "renews_without_payment",
            # What was bought, and what it was priced and granted at.
            "selected_minutes",
            "credit_basis",
            "price_per_minute_inr",
            "credits_per_minute",
        )
        #: Keys answered by this row rather than by the plan. `allocated_credits` is
        #: here because the grant is a fact about this sale: on a minutes-based plan
        #: the plan has no single number to fall back to, and reading one would be
        #: reading somebody else's contract.
        own_columns = {
            "allocated_credits": self.allocated_credits,
            "selected_minutes": self.selected_minutes,
        }

        resolved = {}
        for key in keys:
            if key in snapshot:
                resolved[key] = snapshot[key]
            elif own_columns.get(key) is not None:
                resolved[key] = own_columns[key]
            elif key in own_columns:
                # Deliberately absent rather than guessed. A caller asking about a
                # cycle that recorded nothing gets None, not a plausible number.
                resolved[key] = None
            elif self.plan_id:
                resolved[key] = getattr(self.plan, key, None)
        return resolved

    def included_entitlement(self, resource):
        """Included allowance for one resource, or ``None`` for unlimited."""
        entry = self._entitlement_entry(resource)
        if isinstance(entry, dict):
            return entry.get("included")
        return entry

    def entitlement_reset_period(self, resource):
        """How often this allowance refills for *this* subscription.

        Read from the snapshot, so a plan whose period is changed later does not
        re-interpret an allowance a tenant is already living under. Subscriptions
        written before the period was frozen fall back to the live plan.
        """
        from onboard.constants.reset_period import ResetPeriod

        entry = self._entitlement_entry(resource)
        if isinstance(entry, dict):
            return entry.get("reset_period") or ResetPeriod.NONE
        if self.plan_id:
            row = self.plan.entitlements.filter(resource=resource).first()
            if row:
                return row.reset_period
        return ResetPeriod.NONE

    def _entitlement_entry(self, resource):
        """The frozen allowance, in whichever shape the snapshot was written.

        Older snapshots stored a bare number; newer ones store
        ``{"included": n, "reset_period": ...}``. Both are read here so a migration
        is never required to keep an existing subscription working.
        """
        snapshot = (self.plan_snapshot or {}).get("entitlements")
        if snapshot is not None:
            return snapshot.get(resource)
        if self.plan_id:
            row = self.plan.entitlements.filter(resource=resource).first()
            return row.included_quantity if row else None
        return None

    # --- state ----------------------------------------------------------------

    @property
    def is_current(self):
        return self.status == self.SubscriptionStatusChoices.ACTIVE

    @property
    def has_scheduled_change(self):
        return self.scheduled_plan_id is not None

    @property
    def is_expired(self):
        return bool(self.end_date and self.end_date < timezone.now())

    def save(self, *args, **kwargs):
        # A field default does not apply when a caller passes None explicitly, and
        # this column is non-null. Zero is the honest reading of "granted nothing",
        # and it keeps a caller that has not been updated from taking down a save
        # with an IntegrityError.
        if self.allocated_credits is None:
            self.allocated_credits = 0
        # A subscription to a plan sold by the minute has to have a quantity: without
        # one there is nothing to price it or grant against, and the frozen contract
        # would say the cycle was worth nothing. The plan's smallest sellable amount
        # is the honest default, and the same one `activate()` applies.
        if (
            self.selected_minutes is None
            and self.plan_id
            and self.plan.is_minutes_based
        ):
            self.selected_minutes = self.plan.resolve_minutes(None)
        if not self.plan_snapshot and self.plan:
            self.generate_plan_snapshot()
        # Only ever fills a gap. A renewal that has already pushed end_date forward
        # must never be silently recomputed back to start_date plus one cycle.
        if not self.end_date:
            self.end_date = self.derive_end_date()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.tenant.name} - {self.plan.name} ({self.status})"
