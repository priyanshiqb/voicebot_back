from django.conf import settings
from django.db import models

from tenant.common.models.tenant_base_model_core import TenantBaseModelCore
from utils.models.base_model_core import BaseQuerySet

#: Raised instead of silently mutating history. Not a ValidationError — nothing a
#: caller passed is wrong; the operation itself does not exist for this model.
_IMMUTABLE = (
    "CreditLedger is append-only. Correct a mistake by posting a compensating "
    "ADMIN_ADJUSTMENT entry, never by changing or removing an existing row."
)


class CreditLedgerQuerySet(BaseQuerySet):
    """Queryset that refuses every bulk path that could rewrite history.

    ``BaseQuerySet`` already blocks ``delete()``, but its ``soft_delete()`` issues a
    plain ``UPDATE is_deleted = true`` — and because the default manager filters
    those rows out, a soft-deleted ledger row would vanish from every sum while
    still sitting in the table. That is precisely the failure the balance invariant
    cannot survive, so it is closed here rather than trusted to convention.
    """

    def delete(self):
        raise NotImplementedError(_IMMUTABLE)

    def soft_delete(self):
        raise NotImplementedError(_IMMUTABLE)

    def update(self, **kwargs):
        raise NotImplementedError(_IMMUTABLE)


class CreditLedgerManager(models.Manager):
    """Live rows, with the immutable queryset. Mirrors ``ExceptDeletedManager``."""

    def get_queryset(self):
        return CreditLedgerQuerySet(self.model, using=self._db).filter(is_deleted=False)


class CreditLedgerAllManager(models.Manager):
    """Every row, with the immutable queryset.

    Used for idempotency probes, where the question is "has this reference ever been
    recorded" and a filtered manager could answer "no" about a row that exists.
    """

    def get_queryset(self):
        return CreditLedgerQuerySet(self.model, using=self._db)


class CreditLedger(TenantBaseModelCore):
    """
    One credit movement. Append-only, and the authority for every balance.

    ``Wallet.balance`` is a cache of ``SUM(amount)`` over these rows. That only means
    anything if the rows are genuinely immutable, so immutability is enforced in
    three places: this model's ``save`` refuses updates, the queryset above refuses
    bulk rewrites, and the admin exposes no add, change, or delete.

    **Usage rows carry their own rating snapshot** — ``raw_quantity``, ``units``,
    ``unit_type``, ``rate_applied``. Without it, re-pricing a plan next year silently
    re-explains every historical charge with the new rate, and "why was I charged 10
    credits?" stops having an answer. With it, the answer is on the row.

    **Every row that can be replayed carries a ``reference_id``**, unique per entry
    type. A retried runner POST, a redelivered webhook, or a double-clicked button
    collides with that constraint and is swallowed as a no-op. It is the single most
    important defence here, and it is free for calls because
    ``AgentCallLog.provider_call_id`` is already unique.
    """

    class EntryType(models.TextChoices):
        # Opening a wallet at a known starting point, so the invariant holds from the
        # first commit rather than from the first real transaction.
        OPENING_BALANCE = "opening_balance", "Opening Balance"

        # --- credits in ---
        CYCLE_ALLOCATION = "cycle_allocation", "Cycle Allocation"
        MANUAL_TOP_UP = "manual_top_up", "Manual Top-Up"
        USAGE_REFUND = "usage_refund", "Usage Refund"

        # --- credits out ---
        USAGE_DEDUCTION = "usage_deduction", "Usage Deduction"
        # A duration corrected upward after the fact. A separate type rather than an
        # edit to the original row, because the original charge really did happen.
        USAGE_ADJUSTMENT = "usage_adjustment", "Usage Adjustment"

        # Three distinct forfeits, because "your plan does not roll over", "you hit
        # the rollover cap", and "your subscription lapsed" are three different
        # support conversations, and a single FORFEIT type answers none of them.
        ROLLOVER_FORFEIT = "rollover_forfeit", "Rollover Forfeit"
        ROLLOVER_CAP_FORFEIT = "rollover_cap_forfeit", "Rollover Cap Forfeit"
        EXPIRY_FORFEIT = "expiry_forfeit", "Expiry Forfeit"

        # The sanctioned way for staff to move credits. Without it, someone edits
        # Wallet.balance directly and the invariant is gone within a month.
        ADMIN_ADJUSTMENT = "admin_adjustment", "Admin Adjustment"

    class Bucket(models.TextChoices):
        ALLOCATED = "allocated", "Allocated"
        PURCHASED = "purchased", "Purchased"

    wallet = models.ForeignKey(
        "wallet.Wallet",
        on_delete=models.PROTECT,
        related_name="entries",
        help_text="The wallet this movement belongs to. PROTECT — history outlives everything.",
    )
    entry_type = models.CharField(
        max_length=32,
        choices=EntryType.choices,
        db_index=True,
        help_text="Why this movement happened.",
    )
    bucket = models.CharField(
        max_length=16,
        choices=Bucket.choices,
        default=Bucket.ALLOCATED,
        help_text="Which balance bucket this movement affected.",
    )
    amount = models.IntegerField(
        help_text="Signed: positive adds credits, negative removes them. Never zero.",
    )
    balance_after = models.PositiveIntegerField(
        help_text="The wallet's total balance immediately after this row was written.",
    )

    # --- rating snapshot: set on usage rows, null elsewhere -------------------
    operation = models.CharField(
        max_length=32,
        blank=True,
        null=True,
        help_text="The billable operation, e.g. outbound_call.",
    )
    raw_quantity = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text="Measured quantity before rounding, e.g. 90 (seconds).",
    )
    units = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text="Billable units charged after rounding and minimums, e.g. 2 (minutes).",
    )
    unit_type = models.CharField(
        max_length=32,
        blank=True,
        null=True,
        help_text="What one billable unit was, e.g. per_minute.",
    )
    rate_applied = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text="Credits per billable unit at the time of the charge.",
    )
    rate = models.ForeignKey(
        "wallet.CreditRate",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="ledger_entries",
        help_text=(
            "The catalogue row this charge derived from. SET_NULL because the numbers "
            "above are the record — this is a convenience link, not the source."
        ),
    )

    # --- provenance -----------------------------------------------------------
    # Not a clean join key for usage. A debit that spans both buckets writes two rows,
    # and the second is keyed "{reference}#purchased" (see `_post_debit`). Joining
    # ledger rows to calls on `reference_id = provider_call_id` silently drops that
    # row and undercounts for any tenant holding purchased credits. Match both keys,
    # as `refund()` does, or read `AgentCallLog.credits_charged`, which has the total.
    reference_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        db_index=True,
        help_text=(
            "The real-world event that caused this row — a provider call id, a "
            "Razorpay order id, a source subscription id, a support ticket. Unique "
            "per entry type, which is what makes replays no-ops."
        ),
    )
    subscription = models.ForeignKey(
        "onboard.Subscription",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="ledger_entries",
        help_text=(
            "Which billing cycle was live when this happened. Audit context only — "
            "nothing in this app reads it to make a decision, which is what keeps the "
            "wallet independent of the subscription engine."
        ),
    )
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="credit_ledger_entries",
        help_text="The staff user behind an ADMIN_ADJUSTMENT. Null for system-written rows.",
    )
    notes = models.TextField(
        blank=True,
        null=True,
        help_text="Free text explaining this row, shown to staff.",
    )
    metadata = models.JSONField(
        blank=True,
        null=True,
        help_text="Structured context that does not deserve its own column.",
    )

    objects = CreditLedgerManager()
    all_objects = CreditLedgerAllManager()

    class Meta:
        db_table = "wallet_credit_ledger"
        verbose_name = "Credit Ledger Entry"
        verbose_name_plural = "Credit Ledger"
        ordering = ["-created_at"]
        constraints = [
            # The idempotency guarantee. Partial, so the many rows that legitimately
            # have no reference (an untraceable manual adjustment, say) do not
            # collide with each other.
            models.UniqueConstraint(
                fields=["entry_type", "reference_id"],
                condition=models.Q(reference_id__isnull=False),
                name="uniq_ledger_entry_reference",
            ),
            # A zero-credit movement is either a bug or noise in the audit trail.
            models.CheckConstraint(
                condition=~models.Q(amount=0),
                name="ck_ledger_amount_non_zero",
            ),
        ]
        indexes = [
            models.Index(fields=["wallet", "-created_at"]),
            models.Index(fields=["tenant", "entry_type", "-created_at"]),
            models.Index(fields=["operation", "-created_at"]),
        ]

    # --- append-only enforcement ---------------------------------------------
    # Note for migrations: historical models do not carry these overrides, so a
    # RunPython data migration can create rows normally.

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise NotImplementedError(_IMMUTABLE)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise NotImplementedError(_IMMUTABLE)

    def soft_delete(self, user=None):
        raise NotImplementedError(_IMMUTABLE)

    @property
    def is_credit(self):
        return self.amount > 0

    def __str__(self):
        sign = "+" if self.amount > 0 else ""
        return f"{self.get_entry_type_display()} {sign}{self.amount} → {self.balance_after}"
