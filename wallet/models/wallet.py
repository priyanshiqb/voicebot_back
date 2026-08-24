from django.db import models

from tenant.common.models.tenant_base_model_core import TenantBaseModelCore


class Wallet(TenantBaseModelCore):
    """
    A tenant's credit balance. One row per tenant, for the life of the tenant.

    **The balance here is a cache; the ledger is the truth.** The invariant is
    ``SUM(CreditLedger.amount WHERE wallet = self) == self.balance``, and it holds
    because exactly one place is allowed to write it — ``wallet.services.wallet``,
    which takes ``select_for_update()`` on this row around every mutation. This
    column exists so that authorising a call is one indexed read rather than an
    aggregate over the tenant's entire history.

    **Anchored to the tenant, not the subscription.** Subscriptions come and go —
    every upgrade closes one and opens another — and a balance that moved with them
    would make every plan change a transfer, with a close-out step that has to land
    at exactly zero. Anchoring here means a plan change is only ever a *forfeit*
    decision: the wallet persists, and the rollover rules decide how much of it
    burns. It also means nothing on the consumption path has to work out which
    subscription is currently live.

    **This model knows nothing about subscriptions.** The subscription engine pushes
    into it — deposits an allocation, stamps a rate card, freezes on lapse — and the
    wallet never looks the other way. That inversion is what lets the wallet ship
    before any of the subscription work exists.
    """

    # --- balances -------------------------------------------------------------
    # Two buckets, one total. `balance` is denormalised from the two so the hot
    # path reads a single column; a CheckConstraint below keeps them consistent.
    balance = models.PositiveIntegerField(
        default=0,
        help_text="Total spendable credits. Always allocated_balance + purchased_balance.",
    )
    allocated_balance = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Credits granted by a plan's billing cycle. Rollover rules govern this "
            "bucket only, and consumption drains it first."
        ),
    )
    purchased_balance = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Credits bought with cash via a mid-cycle top-up. Always survive a plan "
            "change — the tenant paid for these directly, and expiring them at a "
            "renewal boundary is a chargeback conversation."
        ),
    )

    # --- lifetime counters ----------------------------------------------------
    # Monotonic, analytics only. Never read to make a decision: the ledger is the
    # authority for anything that has to be correct.
    lifetime_granted = models.PositiveIntegerField(
        default=0,
        help_text="Total credits ever added to this wallet. Only ever increases.",
    )
    lifetime_consumed = models.PositiveIntegerField(
        default=0,
        help_text="Total credits ever spent from this wallet. Only ever increases.",
    )

    # --- spendability ---------------------------------------------------------
    # Set from outside; the wallet has no opinion on why. The subscription engine
    # freezes on lapse and unfreezes on renewal, and support freezes for fraud.
    #
    # Frozen blocks *spending*, not *granting* — a renewal payment has to be able to
    # land in a frozen wallet and then unfreeze it.
    is_frozen = models.BooleanField(
        default=False,
        db_index=True,
        help_text="When true, credits cannot be spent. Grants still land.",
    )
    frozen_reason = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Why spending is blocked, e.g. subscription_expired.",
    )
    frozen_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="When spending was blocked.",
    )

    # --- low balance alerting -------------------------------------------------
    low_balance_threshold = models.PositiveIntegerField(
        default=0,
        help_text="Notify the tenant at or below this balance. 0 disables the alert.",
    )
    low_balance_notified_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text=(
            "When the last low-balance alert was sent. Read by the daily sweep to "
            "avoid alerting the same tenant every day."
        ),
    )

    # --- the plan's rate card, stamped at subscription time -------------------
    # Rates are part of what a tenant buys, so they belong to the plan. But having
    # the rating path resolve "which subscription is active right now" on every call
    # would invert this app's dependencies and put a cross-app join on the hot path.
    #
    # So the plan's rate card is *pushed* here when a subscription activates,
    # renews, or changes plan, and read back off this row at call time. Freezing it
    # also matches how `Subscription.plan_snapshot` already freezes pricing: editing
    # a plan changes terms for future cycles, never for a cycle already paid for.
    current_plan = models.ForeignKey(
        "onboard.Plan",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="wallets",
        help_text=(
            "The plan whose rate card is currently stamped below. Display and "
            "debugging only — rating reads rate_snapshot, not this."
        ),
    )
    rate_snapshot = models.JSONField(
        blank=True,
        null=True,
        help_text=(
            "Frozen copy of the plan's rate card, and the authority for rating every "
            "call. Shape: {plan_id, plan_code, stamped_at, operations: {<operation>: "
            "{unit, credits_per_unit, rounding, minimum_units, rate_id}}}."
        ),
    )
    rate_snapshot_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="When the rate card above was stamped.",
    )

    class Meta:
        db_table = "wallet_wallet"
        verbose_name = "Wallet"
        verbose_name_plural = "Wallets"
        ordering = ["-created_at"]
        constraints = [
            # One live wallet per tenant. `is_deleted=False` so a soft-deleted row
            # never blocks a legitimate replacement.
            models.UniqueConstraint(
                fields=["tenant"],
                condition=models.Q(is_deleted=False),
                name="uniq_wallet_per_tenant",
            ),
            # The denormalised total is not allowed to drift from its parts. This
            # catches a service bug at the database rather than at reconciliation
            # time, by which point the wrong number has already been quoted to
            # somebody.
            models.CheckConstraint(
                condition=models.Q(
                    balance=models.F("allocated_balance") + models.F("purchased_balance")
                ),
                name="ck_wallet_balance_is_sum_of_buckets",
            ),
        ]

    @classmethod
    def for_tenant(cls, tenant_id):
        """This tenant's wallet, creating it if it does not exist yet.

        Mirrors ``TenantSettings.for_tenant`` deliberately, and for the same reason:
        this is read on paths that must not fail — authorising a call, rendering a
        dashboard — so "no row yet" has to behave like "empty wallet", never like an
        error.
        """
        wallet, _ = cls.objects.get_or_create(tenant_id=tenant_id)
        return wallet

    @property
    def is_spendable(self):
        """Whether credits may be spent from this wallet at all.

        Deliberately not a function of any subscription. Whatever decides that a
        lapsed cycle should stop spending calls ``freeze()``; this only reports the
        resulting state.
        """
        return not self.is_frozen and self.balance > 0

    @property
    def is_low(self):
        """Whether the balance has reached the tenant's alert threshold."""
        return bool(self.low_balance_threshold) and self.balance <= self.low_balance_threshold

    def __str__(self):
        return f"{self.tenant} — {self.balance} credits"
