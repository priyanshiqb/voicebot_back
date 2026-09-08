from django.db import models

from utils.models.base_model_core import BaseModelCore


class CreditRate(BaseModelCore):
    """
    What one plan charges for one operation. The **rate catalogue**.

    Plan-scoped, not tenant-scoped: a rate is part of what a tenant buys, so moving
    from Starter to Professional buys a bigger allocation *and* a cheaper per-minute
    rate. There is no platform-wide default — see the note on ``plan`` below.

    **This table is not on the call path.** It is read when a plan's rate card is
    stamped onto a wallet (on activation, renewal, or an applied plan change), and by
    the admin. Rating a call reads ``Wallet.rate_snapshot``, never this. Three layers
    each answer a different question:

    * this table's history — what did we *advertise* on this plan, on this date?
    * ``Wallet.rate_snapshot`` — what is this tenant *entitled to* right now?
    * ``CreditLedger`` rating fields — what was this *specific charge* computed from?

    **Rows are superseded, never edited.** Re-pricing means closing the current row
    (``effective_to = now``) and opening a new one. The uniqueness constraint below
    enforces the half of that which matters: you cannot end up with two open-ended
    rates for the same plan and operation, and therefore never with a coin-flip at
    stamp time.
    """

    class Operation(models.TextChoices):
        # The three call tiers. A minute of talking costs a different number of
        # credits depending on how it was served, and the tier is the only thing that
        # varies — direction is deliberately not priced, so an inbound and an
        # outbound minute on the same tier cost the same. Pricing them separately
        # would mean six operations rather than three.
        #
        # These three values are identical to `core.constants.service_tier
        # .ServiceTier`; see the note there for why the enums are kept separate but
        # string-equal.
        STATIC_REEL = "static_reel", "Static Reel (one-way playback)"
        CONVERSATION_HIGH_LATENCY = (
            "conversation_high_latency",
            "Conversation — Standard (high latency)",
        )
        CONVERSATION_LOW_LATENCY = (
            "conversation_low_latency",
            "Conversation — Realtime (low latency)",
        )

        # Every operation is a call tier, and the set is deliberately closed.
        #
        # Storage is not here: it is a gauge, so it is a PlanEntitlement allowance
        # with a rupee-priced add-on, not credits per GB-month. Charging credits for
        # it would mean owing a refund whenever a tenant deleted a file, which is the
        # question a gauge modelled as a flow can never answer.
        #
        # SMS is not here because it is not sold.

    class Unit(models.TextChoices):
        # Talk time, priced three ways. PER_EVENT is a flat price per call regardless
        # of length; the other two are duration-based.
        PER_EVENT = "per_event", "Per Event"
        PER_SECOND = "per_second", "Per Second"
        PER_MINUTE = "per_minute", "Per Minute"

    class Rounding(models.TextChoices):
        CEIL = "ceil", "Round Up"
        FLOOR = "floor", "Round Down"
        NEAREST = "nearest", "Round To Nearest"

    #: How many of the raw quantity's units make up one billable unit. ``PER_EVENT``
    #: is absent deliberately — it ignores quantity entirely. Kept beside the enum
    #: rather than in the rating service so the two cannot drift.
    UNIT_DIVISORS = {
        Unit.PER_SECOND: 1,
        Unit.PER_MINUTE: 60,
    }

    plan = models.ForeignKey(
        "onboard.Plan",
        on_delete=models.CASCADE,
        related_name="credit_rates",
        help_text=(
            "The plan this rate applies to. Required: a rate with no plan would be a "
            "platform-wide fallback, and the moment one exists 'did this tenant get "
            "the Pro rate or the fallback?' becomes something you investigate rather "
            "than read. Free plans get a full card priced at 0, not missing rows."
        ),
    )
    operation = models.CharField(
        max_length=32,
        choices=Operation.choices,
        db_index=True,
        help_text="The billable operation this rate prices.",
    )
    unit = models.CharField(
        max_length=32,
        choices=Unit.choices,
        default=Unit.PER_MINUTE,
        help_text="What one billable unit is — an event, a second, a minute, a GB-month.",
    )
    credits_per_unit = models.PositiveIntegerField(
        help_text="Credits charged per billable unit. 0 makes the operation free on this plan.",
    )
    price_per_minute_inr = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        blank=True,
        null=True,
        help_text=(
            "Pre-discount, pre-tax rupees per minute of this tier, on this plan. Read "
            "when a plan is *sold* at this tier -- see ``Plan.credit_basis`` -- and "
            "never on the call path.\n\n"
            "**Here rather than on Plan, beside credits_per_unit rather than apart from "
            "it.** What a minute of a tier costs in money and what it costs in credits "
            "are two views of one commercial decision. Split across two tables they can "
            "be changed independently, and switching a plan's basis would double the "
            "credits it grants while leaving the price untouched, with nothing anywhere "
            "to compare the two against.\n\n"
            "Being here also means a money price is **superseded rather than "
            "overwritten**: re-pricing closes this row and opens another, so what a "
            "plan cost last month stays answerable. ``Plan.base_price`` never had that.\n\n"
            "Null on a plan that is not sold in minutes. Four decimal places because "
            "two cannot express the difference between 1.80 and 1.85 on a 2,000-minute "
            "plan without moving 100 rupees of monthly revenue; the charged total is "
            "still quantized to paise."
        ),
    )
    rounding = models.CharField(
        max_length=16,
        choices=Rounding.choices,
        default=Rounding.CEIL,
        help_text=(
            "How a partial unit is resolved. Rounding up is standard telecom billing: "
            "a 90-second call on a per-minute rate bills two minutes."
        ),
    )
    minimum_units = models.PositiveIntegerField(
        default=1,
        help_text=(
            "Smallest billable quantity — the fine print telephony billing always "
            "has. With 1, a 4-second call still bills one minute."
        ),
    )
    effective_from = models.DateTimeField(
        help_text="When this rate started applying to newly stamped wallets.",
    )
    effective_to = models.DateTimeField(
        blank=True,
        null=True,
        help_text="When this rate was superseded. Null means it is the current rate.",
    )
    notes = models.TextField(
        blank=True,
        null=True,
        help_text="Why this rate exists or why it was superseded.",
    )

    class Meta:
        db_table = "wallet_credit_rate"
        verbose_name = "Credit Rate"
        verbose_name_plural = "Credit Rates"
        ordering = ["plan", "operation", "-effective_from"]
        constraints = [
            # At most one open-ended rate per plan and operation. This is what makes
            # supersede-don't-edit stick: publishing a new price without closing the
            # old one fails here instead of producing two "current" rates.
            models.UniqueConstraint(
                fields=["plan", "operation"],
                condition=models.Q(effective_to__isnull=True, is_deleted=False),
                name="uniq_current_rate_per_plan_operation",
            ),
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True)
                | models.Q(effective_to__gt=models.F("effective_from")),
                name="ck_credit_rate_period_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["plan", "operation", "-effective_from"]),
        ]

    @property
    def is_current(self):
        """Whether this row is the open-ended rate for its plan and operation."""
        return self.effective_to is None

    @property
    def unit_divisor(self):
        """Raw-quantity units per billable unit, or ``None`` for per-event rates."""
        return self.UNIT_DIVISORS.get(self.unit)

    def as_snapshot_entry(self):
        """This rate as it appears inside ``Wallet.rate_snapshot["operations"]``.

        Defined here so the stamped JSON shape lives next to the model it is copied
        from; the rating service reads these keys back and nothing else.
        """
        return {
            "unit": self.unit,
            "credits_per_unit": self.credits_per_unit,
            "rounding": self.rounding,
            "minimum_units": self.minimum_units,
            "rate_id": str(self.id),
        }

    def __str__(self):
        window = "current" if self.is_current else f"until {self.effective_to:%Y-%m-%d}"
        return f"{self.operation}: {self.credits_per_unit}/{self.unit} ({window})"
