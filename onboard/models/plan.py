from typing import NamedTuple

from django.db import models
from utils.models.base_model_core import BaseModelCore
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP


#: Largest quantity anything in this system will sell in one cycle. A guard rather
#: than a policy: a mistyped 10**12 should be refused, not carried into a price the
#: `Transaction.amount` column cannot even hold.
MAX_SELECTABLE_MINUTES = 1_000_000

#: What ``Transaction.amount`` — ``DecimalField(max_digits=10, decimal_places=2)`` —
#: can physically store. A quote above this would raise ``DataError`` at insert time,
#: which is a 500 in the middle of a checkout rather than a refusal the customer can
#: read.
MAX_CHARGEABLE_TOTAL = Decimal("99999999.99")


def coerce_minutes(value):
    """``value`` as a positive whole number of minutes, or ``None``. **Never raises.**

    Every minutes-aware method funnels its input through here, because they are
    reached from a dozen places — request bodies, query strings, JSON snapshots,
    management-command prompts — and a quantity is the one field a client is most
    likely to send as a string, a float, or nothing at all.

    ``None`` is the answer for anything unusable, which every caller already handles:
    it is the same state a legacy subscription is in. Raising here instead would turn
    a bad query parameter into a 500 somewhere far from the request that caused it.

    ``bool`` is excluded deliberately -- it is an ``int`` subclass, so ``True`` would
    otherwise quietly become one minute.

    **A fractional quantity is refused, not truncated.** ``int(1.5)`` is 1, and selling
    somebody one minute because they asked for one and a half is worse than telling
    them the number made no sense. An integral float (``1200.0``, which is what JSON
    gives you) is fine.

    **The ceiling is applied here**, so no code path anywhere can carry an absurd
    quantity into a price, a credit grant or a database column. That is the difference
    between a guard and a hope.
    """
    if value is None or isinstance(value, bool):
        return None

    try:
        minutes = int(value)
    except (TypeError, ValueError, OverflowError, ArithmeticError):
        # str, dict, list, object, NaN and infinity all land here. ArithmeticError
        # covers decimal.InvalidOperation, which is not a ValueError.
        return None

    if isinstance(value, float) and not value.is_integer():
        return None
    if isinstance(value, Decimal) and value != minutes:
        return None

    if minutes <= 0 or minutes > MAX_SELECTABLE_MINUTES:
        return None
    return minutes


class PriceComponents(NamedTuple):
    """What a plan costs for a given quantity, broken into the numbers on the invoice.

    ``gross`` is the list price before discount -- the struck-through figure on a
    pricing page. ``net`` is after discount and before tax, which is the value every
    proration in the system is computed against. ``total`` is what goes to the gateway.

    ``net + tax == total`` exactly, by construction: see ``Plan.price_components``.
    """

    gross: Decimal
    discount: Decimal
    net: Decimal
    tax: Decimal
    total: Decimal


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

    class CreditBasis(models.TextChoices):
        """The call tier a plan is *sold* at.

        Both halves of a minutes-based sale are read from the one ``CreditRate`` row
        this names: ``price_per_minute_inr`` for what is charged, ``credits_per_unit``
        for what is granted. Naming a tier rather than storing two loose numbers is
        what stops the price and the grant drifting apart.

        String-equal to ``wallet.CreditRate.Operation`` and deliberately a separate
        enum, exactly as ``core.constants.service_tier.ServiceTier`` already is --
        ``onboard`` does not import ``wallet`` at module scope.

        **The plan picks the basis, not the customer.** That is what keeps plan
        selection a function of minutes alone: at any quantity there is one plan, one
        basis row, one price and one grant.
        """

        STATIC_REEL = "static_reel", "Static Reel (one-way playback)"
        CONVERSATION_HIGH_LATENCY = (
            "conversation_high_latency",
            "Conversation — Standard (high latency)",
        )
        CONVERSATION_LOW_LATENCY = (
            "conversation_low_latency",
            "Conversation — Realtime (low latency)",
        )

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
    # Minutes
    #
    # Which quantities this plan sells, and which tier it sells them at.
    # Together with the basis row's two numbers, these turn a plan from a
    # point on a price list into a band: the customer names a quantity, and
    # the plan, the price and the credit grant all follow from it.
    #
    # A plan with no basis is a *legacy* plan and behaves exactly as it did
    # before -- fixed base_price, fixed allocated_credits. Both generations
    # are sellable at once; see is_minutes_based.
    # ------------------------------------------------------------------ #
    min_minutes = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text=(
            "Fewest minutes this plan sells, inclusive. Null means no floor -- the "
            "plan sells anything up to max_minutes."
        ),
    )
    max_minutes = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text=(
            "Most minutes this plan sells, inclusive. Null means no ceiling -- the "
            "plan sells anything from min_minutes upwards.\n\n"
            "Equal to min_minutes makes the plan a fixed-quantity pack: '5,000 "
            "minutes', not a range. That needs no special handling anywhere -- it is "
            "a band of width one, priced and granted like any other."
        ),
    )
    credit_basis = models.CharField(
        max_length=32,
        choices=CreditBasis.choices,
        blank=True,
        null=True,
        help_text=(
            "Which call tier this plan is sold at. The open CreditRate row for this "
            "operation supplies both the rupees per minute charged and the credits "
            "per minute granted.\n\n"
            "Setting this is what makes a plan minutes-based. Null leaves it on the "
            "legacy fixed-bundle model."
        ),
    )

    # ------------------------------------------------------------------ #
    # Credits
    #
    # What a cycle grants and what happens to leftovers. Per-operation
    # *prices* are not here — they are CreditRate rows pointing at this
    # plan, because prices need version history and these do not: they are
    # frozen into Subscription.plan_snapshot when a tenant subscribes.
    # ------------------------------------------------------------------ #
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
            "Absolute ceiling on credits carried into the next cycle. Null means "
            "uncapped. Without a cap, a light user on an annual plan can bank a year "
            "of credits and then run them down while paying nothing. "
            "**Superseded by max_rollover_ratio**, which is read first. Kept as the "
            "fallback for plans and snapshots written before the ratio existed."
        )
    )
    max_rollover_ratio = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        blank=True,
        null=True,
        help_text=(
            "Ceiling on carried credits, as a multiple of the cycle's own grant. "
            "1.00 means 'at most one cycle's worth'. Null falls back to the absolute "
            "max_rollover_credits. "
            "**The more urgent of the two recalibrations, because this one destroys "
            "credits rather than mis-timing an alert.** A fixed cap of 3,500 forfeits "
            "nothing from a 500-minute customer and silently burns 4,500 credits from "
            "a 2,000-minute one, at every renewal, for as long as nobody notices."
        ),
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
        help_text=(
            "Alert the tenant at or below this absolute balance. 0 disables it. "
            "**Superseded by low_balance_percent**, which is read first. Kept as the "
            "fallback for plans and snapshots written before the percentage existed."
        )
    )
    low_balance_percent = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text=(
            "Alert the tenant when their balance falls below this percentage of the "
            "cycle's grant. Null falls back to the absolute low_balance_threshold. "
            "**A percentage because the grant is no longer the same for everyone on a "
            "plan.** A band running 501-2,000 minutes grants between 2,004 and 8,000 "
            "credits, so one fixed number is 17% of the small customer's balance and "
            "4% of the large one's -- the first is nagged constantly, the second is "
            "warned when they have hours left and no time to act."
        ),
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
            # An inverted band sells nothing and matches nothing, silently: the plan
            # is active, appears on the pricing page, and no quantity can ever select
            # it. The only new single-row fact in the minutes model, so the only one
            # that can be a constraint -- overlaps and gaps need the rest of the
            # ladder and live in the readiness service instead.
            models.CheckConstraint(
                condition=models.Q(min_minutes__isnull=True)
                | models.Q(max_minutes__isnull=True)
                | models.Q(max_minutes__gte=models.F("min_minutes")),
                name="ck_plan_minutes_band_valid",
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

    # --- the catalogue-price properties, now derived rather than stored ------
    #
    # These four were computed from `base_price`, which no longer exists: a plan's
    # price is a function of a quantity, so there is no single figure to report. They
    # are kept because the admin, the dropdown label and several serializers read
    # them, and they now answer for the plan's *smallest sellable quantity* -- the
    # "from ₹X" a pricing page shows.
    #
    # `price_components(minutes)` is what anything selling a specific quantity must
    # use. These are for display only.

    @property
    def _entry_price(self):
        floor = self.min_minutes or (self.max_minutes if self.is_fixed_quantity else 1)
        return self.price_components(floor)

    @property
    def discount_amount(self):
        """Discount on the plan's smallest sellable quantity."""
        return self._entry_price.discount

    @property
    def net_base_price(self):
        """Net price of the plan's smallest sellable quantity, before tax."""
        return self._entry_price.net

    @property
    def tax_amount(self):
        """Tax on the plan's smallest sellable quantity."""
        return self._entry_price.tax

    @property
    def total_price(self):
        """What the plan's smallest sellable quantity costs, all in."""
        return self._entry_price.total

    # ------------------------------------------------------------------ #
    # Minutes-based selling
    #
    # **The entire legacy/v2 split lives in this block and nowhere else.**
    # Callers ask `price_components(minutes)` and `credits_for(minutes)` and
    # get the right answer for either generation of plan; nothing upstream
    # needs to know which kind it is holding. A branch on `is_minutes_based`
    # appearing in a view or a service is a sign something belongs here.
    # ------------------------------------------------------------------ #

    def refresh_from_db(self, *args, **kwargs):
        """Drop the cached basis row along with the field values.

        Without this the cache outlives a refresh, and a plan whose rate was
        superseded elsewhere keeps quoting the old price to a caller that explicitly
        asked for current data — the exact silent-mispricing failure this model is
        built to prevent.
        """
        self._basis_rate_cache = None
        return super().refresh_from_db(*args, **kwargs)

    def basis_rate(self):
        """The open ``CreditRate`` row this plan is sold at, or ``None``.

        Cached per instance, keyed on the basis, so reassigning ``credit_basis`` in
        memory does not hand back the previous tier's row. Cleared by
        ``refresh_from_db()``; a plan held across a rate change without one is as
        stale as any other unrefreshed instance.
        """
        if not self.credit_basis or not self.pk:
            return None

        # Honours a `Prefetch(..., to_attr="open_rates")`, which is how the ladder
        # loads a whole catalogue in two queries instead of one per plan. The public
        # for-minutes endpoint reads every active plan, so the N+1 here would be an
        # unauthenticated one.
        prefetched = getattr(self, "open_rates", None)
        if prefetched is not None:
            return next(
                (r for r in prefetched if r.operation == self.credit_basis), None
            )

        cached = getattr(self, "_basis_rate_cache", None)
        if cached is not None and cached[0] == self.credit_basis:
            return cached[1]

        row = self.credit_rates.filter(
            operation=self.credit_basis,
            is_deleted=False,
            effective_to__isnull=True,
        ).first()
        self._basis_rate_cache = (self.credit_basis, row)
        return row

    def credits_per_minute(self):
        """Credits one minute of the basis tier costs, or ``None``.

        ``rounding`` and ``minimum_units`` are deliberately ignored: they belong to
        rating a *call*, and applying a per-call minimum to a whole cycle's grant
        would inflate it by the number of calls the tenant has not yet made.
        """
        from wallet.models import CreditRate

        row = self.basis_rate()
        if row is None:
            return None
        if row.unit == CreditRate.Unit.PER_MINUTE:
            return row.credits_per_unit
        if row.unit == CreditRate.Unit.PER_SECOND:
            return row.credits_per_unit * 60
        # PER_EVENT is a flat price per call and has no per-minute meaning.
        return None

    @property
    def is_minutes_based(self):
        """Whether this plan is sold by the minute.

        Requires the basis row to supply **both** numbers -- a rupee price and a
        usable per-minute credit rate. A plan that can be priced but not granted
        (a ``per_event`` basis) falls back to the legacy branch rather than selling
        minutes and handing over nothing; activation refuses it either way.
        """
        row = self.basis_rate()
        return (
            row is not None
            and row.price_per_minute_inr is not None
            and self.credits_per_minute() is not None
        )

    @property
    def is_fixed_quantity(self):
        """A pack rather than a range -- '5,000 minutes', one legal quantity.

        Derived, never stored. A flag beside the bounds is a flag that eventually
        disagrees with them.
        """
        return (
            self.min_minutes is not None
            and self.max_minutes is not None
            and self.min_minutes == self.max_minutes
        )

    def contains_minutes(self, minutes):
        """Whether this plan sells exactly ``minutes``. Both bounds inclusive.

        Unusable input is "no", not an exception -- see :func:`coerce_minutes`.
        """
        minutes = coerce_minutes(minutes)
        if minutes is None:
            return False
        if self.min_minutes is not None and minutes < self.min_minutes:
            return False
        if self.max_minutes is not None and minutes > self.max_minutes:
            return False
        return True

    def low_balance_for(self, allocated_credits):
        """The balance at which this plan warns a tenant holding ``allocated_credits``.

        Percentage first, absolute second. Rounded **up**, so a 10% threshold on a
        small grant is never zero -- a threshold of zero silently disables the alert
        for exactly the customers with the least room to spare.
        """
        if self.low_balance_percent and allocated_credits:
            return -(-int(allocated_credits) * int(self.low_balance_percent) // 100)
        return self.low_balance_threshold or 0

    def rollover_cap_for(self, allocated_credits):
        """Most credits that may be carried, or ``None`` for uncapped.

        Rounded **down**: a cap is a limit, and rounding a limit up grants more than
        the plan says.
        """
        if self.max_rollover_ratio is not None and allocated_credits:
            return int(
                (Decimal(self.max_rollover_ratio) * int(allocated_credits))
                .to_integral_value(rounding=ROUND_DOWN)
            )
        return self.max_rollover_credits

    def resolve_minutes(self, minutes):
        """``minutes`` expressed as a quantity **this** plan can actually sell.

        Inside the band it is returned unchanged; below it, the band's minimum; above
        it, the band's maximum. A fixed-quantity pack therefore always resolves to its
        single value.

        For the caller who names a *destination* rather than a quantity -- "move me to
        Scale" -- where the honest reading is "the same usage, on Scale, as far as
        Scale will allow". Plain clamping would be wrong for a pack, which has exactly
        one legal value and no range to clamp within.

        ``None`` for a legacy plan, which sells no quantity at all.
        """
        if not self.is_minutes_based:
            return None
        minutes = coerce_minutes(minutes)
        if minutes is None:
            return self.min_minutes or self.max_minutes
        if self.min_minutes is not None and minutes < self.min_minutes:
            return self.min_minutes
        if self.max_minutes is not None and minutes > self.max_minutes:
            return self.max_minutes
        return int(minutes)

    def credits_for(self, minutes=None):
        """Credits a cycle grants for ``minutes`` on this plan.

        Falls back to the legacy fixed allocation for a plan that is not sold by the
        minute, and for a caller that passes no quantity.
        """
        minutes = coerce_minutes(minutes)
        if minutes is not None and self.is_minutes_based:
            return minutes * self.credits_per_minute()
        # A plan with no basis cannot grant anything. It also cannot be activated,
        # so this is only reached by a historical row -- and its subscription carries
        # its own `allocated_credits`, which is what renewal falls back to.
        return 0

    def price_components(self, minutes=None):
        """What ``minutes`` costs on this plan, as ``PriceComponents``.

        The minutes-based counterpart to the ``discount_amount`` /
        ``net_base_price`` / ``tax_amount`` / ``total_price`` properties above, and
        deliberately identical to them in every respect but where the gross comes
        from: ``minutes x price_per_minute_inr`` instead of ``base_price``.

        **Each component is quantized as it is produced**, so ``net + tax == total``
        exactly. These five numbers are frozen into ``plan_snapshot`` as separate
        strings and read back independently; a total that does not equal the sum of
        its parts is a dispute nobody can settle.
        """
        minutes = coerce_minutes(minutes)
        if minutes is not None and self.is_minutes_based:
            rate = Decimal(str(self.basis_rate().price_per_minute_inr))
            gross = rate * Decimal(minutes)
        else:
            # Same reasoning as `credits_for`: unpriceable rather than free. A plan
            # in this state cannot pass the activation gate, and a quote of zero
            # would be worse than no quote at all.
            gross = Decimal("0.00")

        gross = gross.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        discount_percentage = Decimal(str(self.discount_percentage or 0))
        discount = (gross * discount_percentage / Decimal("100.00")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        net = gross - discount

        tax_percentage = Decimal(str(self.tax_percentage or 0))
        tax = (net * tax_percentage / Decimal("100.00")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        return PriceComponents(
            gross=gross, discount=discount, net=net, tax=tax, total=net + tax
        )

    def __str__(self):
        return self.name
