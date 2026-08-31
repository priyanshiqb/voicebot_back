"""Does a wallet's balance still match its ledger?

    SUM(CreditLedger.amount) == Wallet.balance          (and per bucket)

**One rule, two callers.** ``reconcile_wallets`` sweeps the whole table nightly; the
admin console asks the same question about one tenant while support has them on the
phone. A second copy of this comparison would be worse than no console check at all:
two answers to "does this balance add up?" means the screen and the job can disagree,
and no operator would know which to believe.

**Reports, never repairs.** Correcting drift is a deliberate operator decision made
after the cause is understood — see the command's ``--fix``, which writes an
``ADMIN_ADJUSTMENT`` rather than editing the balance so the correction is itself
auditable.
"""

from django.db.models import Sum

from wallet.models import CreditLedger


def ledger_sums(wallet):
    """This wallet's ledger totals, per bucket. One query.

    Returned in the same shape :func:`bulk_ledger_sums` produces per wallet, so
    ``check`` cannot tell which of the two it was handed.
    """
    rows = (
        CreditLedger.all_objects.filter(wallet=wallet)
        .values("bucket")
        .annotate(total=Sum("amount"))
    )
    return {row["bucket"]: row["total"] or 0 for row in rows}


def bulk_ledger_sums():
    """Ledger totals for **every** wallet, keyed by wallet id. One query.

    What the sweep uses: a full scan is this job's definition, and the readable
    per-wallet version was 3n queries — fine at seventeen wallets, unusable at a
    hundred thousand.
    """
    totals = {}
    for row in (
        CreditLedger.all_objects.values("wallet_id", "bucket").annotate(total=Sum("amount"))
    ):
        entry = totals.setdefault(row["wallet_id"], {})
        entry[row["bucket"]] = row["total"] or 0
    return totals


def check(wallet, *, sums=None):
    """What has drifted on this wallet. An empty list means it reconciles.

    ``sums`` lets a caller that has already aggregated the whole table pass this
    wallet's slice in, so the sweep keeps its two-queries-for-everything property.
    Callers with one wallet omit it and pay for one aggregate.

    Each problem is a dict rather than a string so the console can render the two
    numbers separately, and ``message`` is the line the command has always printed.
    """
    if sums is None:
        sums = ledger_sums(wallet)

    allocated = sums.get(CreditLedger.Bucket.ALLOCATED, 0)
    purchased = sums.get(CreditLedger.Bucket.PURCHASED, 0)
    total = allocated + purchased

    problems = []
    for field, wallet_value, ledger_value in (
        ("balance", wallet.balance, total),
        ("allocated_balance", wallet.allocated_balance, allocated),
        ("purchased_balance", wallet.purchased_balance, purchased),
    ):
        if wallet_value != ledger_value:
            label = field.replace("_balance", "") if field != "balance" else "balance"
            problems.append({
                "field": field,
                "wallet": wallet_value,
                "ledger": ledger_value,
                "drift": ledger_value - wallet_value,
                "message": f"{label} {wallet_value} vs ledger {ledger_value}",
            })
    return problems


def ledger_total(wallet, *, sums=None):
    """What the ledger says the balance should be."""
    if sums is None:
        sums = ledger_sums(wallet)
    return (
        sums.get(CreditLedger.Bucket.ALLOCATED, 0)
        + sums.get(CreditLedger.Bucket.PURCHASED, 0)
    )
