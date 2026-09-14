"""The only thing allowed to move a credit balance.

Nothing outside this module writes ``Wallet.balance`` or inserts a ``CreditLedger``
row. That single rule is what makes the invariant ``SUM(ledger) == balance``
something you can rely on rather than hope for, because there is exactly one place
where it can be broken and exactly one place to look when it is.

Every mutation follows the same shape:

1. ``atomic()`` and ``select_for_update()`` on the wallet row — without the lock, two
   concurrent calls both read a sufficient balance and both spend it.
2. An idempotency probe on ``(entry_type, reference_id)`` — a retried webhook or a
   re-posted call must be a no-op, not a second charge.
3. Bucket arithmetic, then the denormalised total, then the ledger row.

Replays return ``None`` rather than raising, so callers do not need a try/except
around the ordinary case of Razorpay delivering the same event twice.
"""

import logging
from dataclasses import dataclass

from django.db import transaction as db_transaction
from django.utils import timezone

from wallet.models.credit_ledger import CreditLedger
from wallet.models.wallet import Wallet
from wallet.services import rating
from wallet.services.exceptions import InsufficientCredits, WalletFrozen

logger = logging.getLogger(__name__)

Bucket = CreditLedger.Bucket
EntryType = CreditLedger.EntryType


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

def for_tenant(tenant):
    """This tenant's wallet, creating it if it does not exist yet."""
    return Wallet.for_tenant(_tenant_id(tenant))


def balance_for(tenant):
    """A summary safe to render straight into an API response."""
    wallet = for_tenant(tenant)
    return {
        "balance": wallet.balance,
        "allocated": wallet.allocated_balance,
        "purchased": wallet.purchased_balance,
        "is_frozen": wallet.is_frozen,
        "frozen_reason": wallet.frozen_reason,
        "is_low": wallet.is_low,
        "low_balance_threshold": wallet.low_balance_threshold,
        "plan_code": (wallet.rate_snapshot or {}).get("plan_code"),
    }


def _tenant_id(tenant):
    return getattr(tenant, "id", tenant)


def _lock(tenant):
    """The wallet row, locked for the rest of the transaction."""
    tenant_id = _tenant_id(tenant)
    Wallet.for_tenant(tenant_id)  # ensure it exists before locking
    return Wallet.objects.select_for_update().get(tenant_id=tenant_id)


def _already_recorded(entry_type, reference_id):
    """Whether this exact movement has been written before.

    ``all_objects`` deliberately: the question is "has this reference ever been
    recorded", and a manager that hides rows could answer "no" about a row that
    exists — which is how you double-charge.
    """
    if not reference_id:
        return False
    return CreditLedger.all_objects.filter(
        entry_type=entry_type, reference_id=reference_id
    ).exists()


def _sync_total(wallet):
    wallet.balance = wallet.allocated_balance + wallet.purchased_balance


# --------------------------------------------------------------------------- #
# Credits in
# --------------------------------------------------------------------------- #

@db_transaction.atomic
def grant(
    tenant,
    *,
    entry_type,
    amount,
    bucket=Bucket.ALLOCATED,
    reference_id=None,
    subscription=None,
    performed_by=None,
    notes="",
    metadata=None,
):
    """Add credits. Returns the ledger row, or ``None`` if this is a replay.

    Works on a **frozen** wallet on purpose: a renewal payment has to be able to land
    before the thing that unfreezes it runs, and refusing the deposit would strand a
    tenant who has just paid.
    """
    amount = int(amount)
    if amount <= 0:
        raise ValueError("grant() adds credits; amount must be positive.")

    if _already_recorded(entry_type, reference_id):
        logger.info("replayed grant ignored: %s / %s", entry_type, reference_id)
        return None

    wallet = _lock(tenant)

    if bucket == Bucket.PURCHASED:
        wallet.purchased_balance += amount
    else:
        wallet.allocated_balance += amount
    wallet.lifetime_granted += amount
    _sync_total(wallet)
    wallet.save(update_fields=[
        "allocated_balance", "purchased_balance", "balance", "lifetime_granted", "updated_at",
    ])

    return CreditLedger.objects.create(
        tenant_id=wallet.tenant_id,
        wallet=wallet,
        entry_type=entry_type,
        bucket=bucket,
        amount=amount,
        balance_after=wallet.balance,
        reference_id=reference_id,
        subscription=subscription,
        performed_by=performed_by,
        notes=notes,
        metadata=metadata,
    )


# --------------------------------------------------------------------------- #
# Credits out
# --------------------------------------------------------------------------- #

def _plan_debit(wallet, total):
    """How a debit splits across the two buckets.

    Allocated drains first, so the credits the tenant paid cash for are the ones that
    survive to the next cycle. Returns ``[(bucket, amount), ...]``, capped at what the
    wallet actually holds — callers check affordability separately, and this must
    never drive a balance negative.
    """
    remaining = min(int(total), wallet.balance)
    split = []

    from_allocated = min(remaining, wallet.allocated_balance)
    if from_allocated:
        split.append((Bucket.ALLOCATED, from_allocated))
        remaining -= from_allocated

    if remaining:
        split.append((Bucket.PURCHASED, min(remaining, wallet.purchased_balance)))

    return [(b, a) for b, a in split if a > 0]


def _post_debit(wallet, split, *, entry_type, reference_id, rating_result=None,
                count_as_consumption=True, **row):
    """Write one ledger row per bucket the debit touched.

    A charge that spans buckets gets **two rows**, not one. A single row claiming
    ``bucket=allocated`` for credits that partly came from the purchased bucket would
    be a false statement in an audit trail whose entire job is explaining the balance
    — and it would break any per-bucket reconciliation. The rows share a transaction,
    so the idempotency probe on the primary reference covers both.
    """
    entries = []
    for index, (bucket, amount) in enumerate(split):
        if bucket == Bucket.PURCHASED:
            wallet.purchased_balance -= amount
        else:
            wallet.allocated_balance -= amount
        if count_as_consumption:
            # A forfeit removes credits but nothing was *used*; counting it would
            # overstate every usage report built on this figure.
            wallet.lifetime_consumed += amount
        _sync_total(wallet)

        entries.append(CreditLedger(
            tenant_id=wallet.tenant_id,
            wallet=wallet,
            entry_type=entry_type,
            bucket=bucket,
            amount=-amount,
            balance_after=wallet.balance,
            # Only the first row carries the bare reference. The suffix keeps the
            # unique constraint satisfied while staying obviously derived.
            reference_id=reference_id if index == 0 else f"{reference_id}#{bucket}",
            operation=rating_result.operation if rating_result else None,
            raw_quantity=rating_result.raw_quantity if rating_result else None,
            units=rating_result.units if rating_result else None,
            unit_type=rating_result.unit_type if rating_result else None,
            rate_applied=rating_result.rate_applied if rating_result else None,
            rate_id=rating_result.rate_id if rating_result else None,
            **row,
        ))

    wallet.save(update_fields=[
        "allocated_balance", "purchased_balance", "balance", "lifetime_consumed", "updated_at",
    ])
    for entry in entries:
        entry.save()
    return entries


@dataclass(frozen=True)
class ConsumeResult:
    """What a charge did, in full.

    ``consume()`` returns only the primary ledger row, and for a charge that drew on both
    buckets that row's ``amount`` is the allocated share alone — so a caller cannot learn
    the total from it. ``credits`` is that total.

    ``entries`` is empty for a charge rated at zero: the call was priced and nothing
    moved. A caller that must tell "free" from "never charged" needs exactly that
    distinction, which is why this is returned for a free call rather than ``None``.
    """

    credits: int
    entries: tuple = ()

    @property
    def entry(self):
        """The primary ledger row, the one carrying the bare reference, or ``None``."""
        return self.entries[0] if self.entries else None


@db_transaction.atomic
def consume_detailed(tenant, *, operation, quantity, reference_id, subscription=None,
                     metadata=None):
    """Rate a quantity and deduct it. Returns a :class:`ConsumeResult`, or ``None``.

    ``None`` means one of two harmless things: this reference was already charged, or
    the wallet could not cover it while enforcement is off. Both are logged. A charge
    rated at zero is *not* ``None`` — see :class:`ConsumeResult`.

    Deliberately separate from :func:`assert_can_consume`, which runs *before* the
    work starts. Authorising and charging are different questions, and conflating
    them is how credit systems leak: the runner is an external service that can fail
    and retry, so only a post-facto keyed deduction is safe as the source of truth.
    """
    if _already_recorded(EntryType.USAGE_DEDUCTION, reference_id):
        logger.info("replayed consume ignored: %s", reference_id)
        return None

    wallet = _lock(tenant)
    result = rating.rate_quantity(rating.resolve_rate(wallet, operation), quantity)

    if result.credits <= 0:
        logger.info("consume of 0 credits skipped: %s (%s)", reference_id, operation)
        return ConsumeResult(credits=0)

    if wallet.balance < result.credits:
        # Metering still records what happened elsewhere; here there is nothing to
        # deduct. assert_can_consume is what refuses the work, and only when
        # enforcement is on.
        logger.warning(
            "wallet %s short for %s: needs %s, holds %s",
            wallet.pk, reference_id, result.credits, wallet.balance,
        )
        return None

    entries = _post_debit(
        wallet,
        _plan_debit(wallet, result.credits),
        entry_type=EntryType.USAGE_DEDUCTION,
        reference_id=reference_id,
        rating_result=result,
        subscription=subscription,
        metadata=metadata,
    )
    return ConsumeResult(credits=result.credits, entries=tuple(entries))


def consume(tenant, *, operation, quantity, reference_id, subscription=None, metadata=None):
    """Rate a quantity and deduct it. Returns the primary ledger row, or ``None``.

    ``None`` means nothing was deducted: a replay, a wallet too short to cover it, or a
    charge that rated at zero. The row returned carries only its own bucket's share, so
    for the full total of a charge that spans both buckets use :func:`consume_detailed`.

    **Not itself atomic, on purpose.** ``consume_detailed`` already is; a second
    transaction here would add a savepoint to every charge for nothing, and the
    query-budget tests count those statements.
    """
    result = consume_detailed(
        tenant, operation=operation, quantity=quantity, reference_id=reference_id,
        subscription=subscription, metadata=metadata,
    )
    return result.entry if result else None


@db_transaction.atomic
def forfeit(tenant, *, entry_type, amount=None, bucket=Bucket.ALLOCATED, reference_id,
            notes="", subscription=None):
    """Burn credits without giving anything back — a rollover or expiry forfeit.

    ``amount=None`` forfeits the whole of ``bucket``, which is what expiry does.
    """
    if _already_recorded(entry_type, reference_id):
        return None

    wallet = _lock(tenant)
    held = wallet.purchased_balance if bucket == Bucket.PURCHASED else wallet.allocated_balance
    amount = held if amount is None else min(int(amount), held)

    if amount <= 0:
        return None

    entries = _post_debit(
        wallet,
        [(bucket, amount)],
        entry_type=entry_type,
        reference_id=reference_id,
        count_as_consumption=False,
        notes=notes,
        subscription=subscription,
    )
    return entries[0] if entries else None


@db_transaction.atomic
def refund(tenant, *, reference_id, notes="", metadata=None):
    """Return the credits a usage deduction took, to the buckets they came from.

    Reverses per original row, so a charge that spanned buckets is restored exactly as
    it was drawn rather than dumped into one.
    """
    originals = list(
        CreditLedger.all_objects.filter(
            entry_type=EntryType.USAGE_DEDUCTION
        ).filter(reference_id__in=[reference_id, f"{reference_id}#{Bucket.PURCHASED}"])
    )
    if not originals:
        logger.warning("refund requested for unknown reference %s", reference_id)
        return None

    refund_reference = f"{reference_id}:refund"
    if _already_recorded(EntryType.USAGE_REFUND, refund_reference):
        return None

    wallet = _lock(tenant)
    entries = []
    for index, original in enumerate(originals):
        amount = abs(original.amount)
        if original.bucket == Bucket.PURCHASED:
            wallet.purchased_balance += amount
        else:
            wallet.allocated_balance += amount
        wallet.lifetime_consumed = max(0, wallet.lifetime_consumed - amount)
        _sync_total(wallet)
        entries.append(CreditLedger(
            tenant_id=wallet.tenant_id,
            wallet=wallet,
            entry_type=EntryType.USAGE_REFUND,
            bucket=original.bucket,
            amount=amount,
            balance_after=wallet.balance,
            reference_id=refund_reference if index == 0 else f"{refund_reference}#{original.bucket}",
            operation=original.operation,
            notes=notes,
            metadata=metadata,
        ))

    wallet.save(update_fields=[
        "allocated_balance", "purchased_balance", "balance", "lifetime_consumed", "updated_at",
    ])
    for entry in entries:
        entry.save()
    return entries[0] if entries else None


@db_transaction.atomic
def adjust(tenant, *, amount, reference_id, performed_by=None, notes="",
           bucket=Bucket.PURCHASED):
    """A staff correction, positive or negative.

    The sanctioned way for support to move credits. Without it, someone edits
    ``Wallet.balance`` directly and the invariant is gone within a month.

    Defaults to the purchased bucket: a goodwill grant should behave like something
    the tenant paid for and survive the next renewal, rather than evaporating at a
    cycle boundary and generating a second complaint.
    """
    amount = int(amount)
    if amount == 0:
        raise ValueError("An adjustment of zero is not a movement.")

    if amount > 0:
        return grant(
            tenant,
            entry_type=EntryType.ADMIN_ADJUSTMENT,
            amount=amount,
            bucket=bucket,
            reference_id=reference_id,
            performed_by=performed_by,
            notes=notes,
        )

    if _already_recorded(EntryType.ADMIN_ADJUSTMENT, reference_id):
        return None

    wallet = _lock(tenant)
    split = _plan_debit(wallet, abs(amount))
    if not split:
        return None

    entries = _post_debit(
        wallet, split,
        entry_type=EntryType.ADMIN_ADJUSTMENT,
        reference_id=reference_id,
        performed_by=performed_by,
        notes=notes,
    )
    return entries[0] if entries else None


# --------------------------------------------------------------------------- #
# Authorising
# --------------------------------------------------------------------------- #

def _enforced(tenant):
    """Whether a shortfall actually blocks work for this tenant.

    A plain read rather than ``TenantSettings.for_tenant()``, which would create a row
    — this runs before every call, and a read path should not write.
    """
    from tenant.models import TenantSettings

    return bool(
        TenantSettings.objects.filter(tenant_id=_tenant_id(tenant))
        .values_list("credits_enforced", flat=True)
        .first()
    )


def quote(tenant, *, operation, quantity):
    """What this would cost. No side effects, no lock."""
    wallet = for_tenant(tenant)
    return rating.rate_quantity(rating.resolve_rate(wallet, operation), quantity)


def assert_can_consume(tenant, *, operation, quantity=60):
    """Refuse the work if the tenant cannot pay for it — but only if enforcement is on.

    While ``credits_enforced`` is false this logs what *would* have been refused and
    returns. That is the whole point of metering before blocking: a full cycle of real
    traffic proves the rates are right before a wrong number stops a paying customer's
    campaign.
    """
    wallet = for_tenant(tenant)
    result = rating.rate_quantity(rating.resolve_rate(wallet, operation), quantity)
    enforced = _enforced(tenant)

    if wallet.is_frozen:
        message = f"wallet {wallet.pk} is frozen ({wallet.frozen_reason})"
        if enforced:
            raise WalletFrozen(message)
        logger.warning("would refuse (%s): %s", operation, message)
        return

    if wallet.balance < result.credits:
        if enforced:
            raise InsufficientCredits(result.credits, wallet.balance)
        logger.warning(
            "would refuse (%s): wallet %s needs %s, holds %s",
            operation, wallet.pk, result.credits, wallet.balance,
        )


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

@db_transaction.atomic
def freeze(tenant, *, reason):
    wallet = _lock(tenant)
    if wallet.is_frozen:
        return wallet
    wallet.is_frozen = True
    wallet.frozen_reason = reason
    wallet.frozen_at = timezone.now()
    wallet.save(update_fields=["is_frozen", "frozen_reason", "frozen_at", "updated_at"])
    return wallet


@db_transaction.atomic
def unfreeze(tenant):
    wallet = _lock(tenant)
    if not wallet.is_frozen:
        return wallet
    wallet.is_frozen = False
    wallet.frozen_reason = None
    wallet.frozen_at = None
    wallet.save(update_fields=["is_frozen", "frozen_reason", "frozen_at", "updated_at"])
    return wallet


@db_transaction.atomic
def set_plan_context(tenant, plan, *, at=None, low_balance_threshold=None):
    """Stamp a plan's rate card and alert threshold onto the wallet.

    Call this **before** granting the cycle's credits. A grant that lands first is
    credited while the wallet still carries the previous plan's prices, and any call
    in between is rated at the wrong tier.

    ``low_balance_threshold`` is passed in because the plan alone can no longer answer
    it: on a minutes-based plan the alert is a percentage of *this* subscription's
    grant, and the plan does not know what that was. Omitted, it falls back to the
    plan's absolute figure, which is what every legacy caller wants.
    """
    wallet = _lock(tenant)
    rating.stamp(wallet, plan, at=at, save=False)
    wallet.low_balance_threshold = (
        plan.low_balance_threshold
        if low_balance_threshold is None
        else low_balance_threshold
    )
    wallet.save(update_fields=[
        "rate_snapshot", "rate_snapshot_at", "current_plan",
        "low_balance_threshold", "updated_at",
    ])
    return wallet
