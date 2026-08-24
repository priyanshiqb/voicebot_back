"""Turning usage into credits, and the plan's rate card into a wallet stamp.

Two directions, and they touch different tables on purpose:

* ``build_snapshot`` / ``stamp`` read the ``CreditRate`` catalogue. Called when a
  subscription activates, renews, or changes plan — a handful of times per tenant per
  year.
* ``resolve_rate`` / ``rate_quantity`` read the wallet's own stamped snapshot. Called
  on every single call. No query, no join.

Pushing the rate card onto the wallet rather than pulling it at call time is what
keeps this app free of any dependency on subscriptions, and keeps the hot path to a
single row that is already locked and in memory.
"""

import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from wallet.models.credit_rate import CreditRate
from wallet.services.exceptions import RateNotConfigured

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedRate:
    """A price, however it was resolved. Mirrors the keys stored in a stamp."""

    operation: str
    unit: str
    credits_per_unit: int
    rounding: str
    minimum_units: int
    rate_id: str | None = None
    source: str = "snapshot"  # snapshot | live_plan


@dataclass(frozen=True)
class RatingResult:
    """What a quantity costs, and every number behind it.

    Carries exactly the fields ``CreditLedger`` snapshots, so the quote endpoint, the
    consumption path and the ledger row all render from one computation instead of
    three that can disagree.
    """

    credits: int
    units: int
    raw_quantity: int
    unit_type: str
    rate_applied: int
    operation: str
    rate_id: str | None = None


def build_snapshot(plan, *, at=None, rate_model=CreditRate):
    """The plan's current rate card, in the shape a wallet stores it.

    The only place outside admin that reads the catalogue. ``rate_model`` is injectable
    so a data migration can pass its historical model.
    """
    at = at or timezone.now()
    operations = {}

    rates = rate_model.objects.filter(
        plan=plan, is_deleted=False, effective_from__lte=at
    ).exclude(effective_to__lte=at)

    for rate in rates:
        # Belt and braces against a catalogue that somehow holds two open rows for one
        # operation: the newest wins, matching what a human would assume.
        existing = operations.get(rate.operation)
        if existing and existing["_effective_from"] >= rate.effective_from:
            continue
        operations[rate.operation] = {
            "unit": rate.unit,
            "credits_per_unit": rate.credits_per_unit,
            "rounding": rate.rounding,
            "minimum_units": rate.minimum_units,
            "rate_id": str(rate.pk),
            "_effective_from": rate.effective_from,
        }

    for entry in operations.values():
        entry.pop("_effective_from", None)

    return {
        "plan_id": str(plan.pk),
        "plan_code": plan.code,
        "stamped_at": at.isoformat(),
        "operations": operations,
    }


def stamp(wallet, plan, *, at=None, save=True):
    """Copy a plan's rate card onto a wallet.

    Call this **before** granting a cycle's credits, not after: a grant that lands
    first is credited while the wallet still carries the previous plan's prices, and
    any call in between is rated wrongly.
    """
    at = at or timezone.now()
    wallet.rate_snapshot = build_snapshot(plan, at=at)
    wallet.rate_snapshot_at = at
    wallet.current_plan = plan
    if save:
        wallet.save(update_fields=["rate_snapshot", "rate_snapshot_at", "current_plan", "updated_at"])
    return wallet


def resolve_rate(wallet, operation):
    """The price this wallet pays for one operation.

    Order of resolution:

    1. The wallet's stamp. This is the answer in every normal case, and costs nothing.
    2. The live plan's current catalogue rate, with a warning — a wallet without a
       stamp means some subscription path skipped ``set_plan_context``, and that is
       worth knowing about rather than silently papering over.
    3. ``RateNotConfigured``.
    """
    snapshot = (wallet.rate_snapshot or {}).get("operations") or {}
    entry = snapshot.get(operation)
    if entry:
        return ResolvedRate(
            operation=operation,
            unit=entry["unit"],
            credits_per_unit=entry["credits_per_unit"],
            rounding=entry.get("rounding", CreditRate.Rounding.CEIL),
            minimum_units=entry.get("minimum_units", 1),
            rate_id=entry.get("rate_id"),
            source="snapshot",
        )

    if wallet.current_plan_id:
        logger.warning(
            "wallet %s has no stamped rate for %s; falling back to the live plan. "
            "A subscription path likely skipped set_plan_context().",
            wallet.pk,
            operation,
        )
        now = timezone.now()
        rate = (
            CreditRate.objects.filter(
                plan_id=wallet.current_plan_id,
                operation=operation,
                is_deleted=False,
                effective_from__lte=now,
            )
            .exclude(effective_to__lte=now)
            .order_by("-effective_from")
            .first()
        )
        if rate:
            return ResolvedRate(
                operation=operation,
                unit=rate.unit,
                credits_per_unit=rate.credits_per_unit,
                rounding=rate.rounding,
                minimum_units=rate.minimum_units,
                rate_id=str(rate.pk),
                source="live_plan",
            )

    raise RateNotConfigured(
        f"No rate for '{operation}' on wallet {wallet.pk} "
        f"(plan={wallet.current_plan_id or 'unset'})."
    )


def _round_units(raw_units, mode):
    """Resolve a fractional unit count to a whole one."""
    value = Decimal(raw_units)
    if mode == CreditRate.Rounding.FLOOR:
        quantized = value.to_integral_value(rounding="ROUND_FLOOR")
    elif mode == CreditRate.Rounding.NEAREST:
        quantized = value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    else:  # CEIL, and the default for anything unrecognised
        quantized = value.to_integral_value(rounding="ROUND_CEILING")
    return int(quantized)


def rate_quantity(resolved, raw_quantity):
    """What ``raw_quantity`` of ``resolved.operation`` costs.

    ``raw_quantity`` is in the operation's natural unit — seconds, for a call. A
    per-event rate ignores it entirely and charges once.

        90 seconds, 5 credits/minute, ceil, minimum 1
          → ceil(90/60) = 2 units → 10 credits
    """
    raw_quantity = max(int(raw_quantity or 0), 0)

    if resolved.unit == CreditRate.Unit.PER_EVENT:
        units = max(1, resolved.minimum_units)
    else:
        divisor = CreditRate.UNIT_DIVISORS.get(resolved.unit)
        if not divisor:
            raise RateNotConfigured(
                f"Unit '{resolved.unit}' has no divisor and is not per-event."
            )
        units = _round_units(Decimal(raw_quantity) / Decimal(divisor), resolved.rounding)
        units = max(units, resolved.minimum_units)

    return RatingResult(
        credits=units * resolved.credits_per_unit,
        units=units,
        raw_quantity=raw_quantity,
        unit_type=resolved.unit,
        rate_applied=resolved.credits_per_unit,
        operation=resolved.operation,
        rate_id=resolved.rate_id,
    )


def quote(wallet, operation, raw_quantity):
    """Resolve and rate in one step. No side effects."""
    return rate_quantity(resolve_rate(wallet, operation), raw_quantity)
