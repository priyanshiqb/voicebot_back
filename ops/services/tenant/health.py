"""Is anything wrong with this tenant?

**The block that makes the 360 worth opening.** Everything else on that screen is a
record somebody could eventually have found; this answers the question support would
otherwise have to know to ask, and three of the four checks catch failures that are
silent by construction.

**Nothing here reimplements a rule.** The wallet comparison lives in
``wallet.services.reconciliation`` and the unrated-call query in
``wallet.tasks.unrated_calls``, both shared with the jobs that already run them
nightly. Two answers to "does this balance add up?" would mean the screen and the
sweep could disagree, and no operator would know which to believe.
"""

from datetime import timedelta

from django.utils import timezone

from onboard.models import Transaction
from wallet.services import reconciliation
from wallet.tasks.unrated_calls import unrated_for

#: How far back the payment check looks. A failed card from eight months ago is
#: history; one from last week is why the customer is emailing.
PAYMENT_WINDOW_DAYS = 30

#: How far back the unrated-call sweep looks, matching the nightly job's default.
UNRATED_WINDOW_HOURS = 24

#: What `subscriptions.expire()` writes when it freezes a lapsed wallet. The
#: consistency check keys on it so an unrelated freeze — fraud, a support hold — is
#: not mistaken for a lapse that was handled.
LAPSE_FREEZE_REASON = "subscription_expired"


def _check(code, ok, detail, **extra):
    return {"code": code, "ok": ok, "detail": detail, **extra}


def wallet_reconciles(wallet):
    """``SUM(CreditLedger.amount) == Wallet.balance``, per bucket.

    The balance is a cache and the ledger is the truth. Until this screen existed the
    only thing checking they agree was a management command somebody had to remember
    to run.
    """
    if wallet is None:
        return _check(
            "WALLET_RECONCILES", True,
            "No wallet yet. Nothing to reconcile.",
        )

    problems = reconciliation.check(wallet)
    if not problems:
        return _check(
            "WALLET_RECONCILES", True,
            f"Balance of {wallet.balance} matches the ledger.",
        )
    return _check(
        "WALLET_RECONCILES", False,
        "The wallet balance does not match its ledger: "
        + "; ".join(p["message"] for p in problems),
        problems=problems,
    )


def no_unrated_calls(tenant_id):
    """Calls that completed but were never charged. Revenue quietly not collected."""
    rows = unrated_for(tenant_id, hours=UNRATED_WINDOW_HOURS)
    if not rows:
        return _check(
            "NO_UNRATED_CALLS", True,
            f"Every call in the last {UNRATED_WINDOW_HOURS}h was charged.",
        )
    return _check(
        "NO_UNRATED_CALLS", False,
        f"{len(rows)} call(s) in the last {UNRATED_WINDOW_HOURS}h completed but were "
        "never charged.",
        calls=[
            {"provider_call_id": call_id, "duration_seconds": duration, "at": at}
            for call_id, _tenant_id, duration, at in rows[:10]
        ],
    )


def no_failed_payments(tenant_id):
    """Money that did not land, or landed short.

    ``UNDERPAID`` is its own state deliberately — nothing was granted and a human has
    to decide between refunding and collecting the difference — so it belongs here
    beside an outright failure rather than being filtered out as "not really failed".
    """
    since = timezone.now() - timedelta(days=PAYMENT_WINDOW_DAYS)
    rows = list(
        Transaction.objects.filter(
            tenant_id=tenant_id,
            created_at__gte=since,
            status__in=[
                Transaction.TransactionStatusChoices.FAILED,
                Transaction.TransactionStatusChoices.UNDERPAID,
            ],
        )
        .order_by("-created_at")
        .values("razorpay_order_id", "status", "amount", "transaction_type", "created_at")[:10]
    )
    if not rows:
        return _check(
            "NO_FAILED_PAYMENTS", True,
            f"No failed or underpaid orders in the last {PAYMENT_WINDOW_DAYS} days.",
        )
    return _check(
        "NO_FAILED_PAYMENTS", False,
        f"{len(rows)} failed or underpaid order(s) in the last "
        f"{PAYMENT_WINDOW_DAYS} days.",
        payments=[
            {**row, "amount": str(row["amount"])} for row in rows
        ],
    )


def lapse_consistent(subscription, wallet):
    """Does the wallet's freeze state agree with the subscription's?

    The only check here that has never existed anywhere, and the one that catches the
    failure which produces a furious customer: a cycle that ended while ``freeze()``
    did not run, so the tenant kept spending credits they no longer had a cycle for.

    The reverse — a renewed subscription whose wallet is still frozen *for the lapse*
    — is the same bug from the other side, and locks a paying customer out.
    """
    if subscription is None or wallet is None:
        return _check(
            "LAPSE_CONSISTENT", True,
            "No subscription or no wallet. Nothing to be inconsistent with.",
        )

    Status = subscription.SubscriptionStatusChoices
    frozen_for_lapse = wallet.is_frozen and wallet.frozen_reason == LAPSE_FREEZE_REASON

    # Keyed on `status`, not on `end_date`. A cycle whose date passed an hour ago is
    # waiting for the expiry job, which is not a bug — and reporting it as one would
    # light this check up for every lapsing tenant nightly until the sweep ran.
    if subscription.status == Status.EXPIRED and not wallet.is_frozen:
        # ...unless the plan says so. `expire()` deliberately leaves the wallet
        # spendable when `allow_use_after_expiry` is set: that is how pay-as-you-go
        # behaves, and flagging it would report a working plan as broken.
        if not subscription.rules.get("allow_use_after_expiry", False):
            return _check(
                "LAPSE_CONSISTENT", False,
                "The subscription expired on "
                f"{subscription.end_date:%Y-%m-%d} but the wallet is not frozen — "
                "this tenant can still spend credits they no longer have a cycle for.",
            )

    if subscription.is_current and frozen_for_lapse:
        return _check(
            "LAPSE_CONSISTENT", False,
            "The subscription is active but the wallet is still frozen for a lapsed "
            "cycle — this tenant is paying and locked out.",
        )

    return _check(
        "LAPSE_CONSISTENT", True,
        "The wallet's freeze state agrees with the subscription.",
    )


#: "Not supplied", as distinct from "supplied and genuinely absent". Defaulting to
#: ``None`` would make the 360 — which has already established that a tenant has no
#: wallet — pay for the same two lookups again to rediscover it.
_UNSET = object()


def report(tenant_id, *, subscription=_UNSET, wallet=_UNSET):
    """All four checks. ``ok`` is true only when every one of them is.

    ``subscription`` and ``wallet`` are passed in by the 360, which has already
    loaded them; the standalone endpoint omits them and pays for two reads.
    """
    if wallet is _UNSET:
        from wallet.models import Wallet
        wallet = Wallet.objects.filter(tenant_id=tenant_id, is_deleted=False).first()

    if subscription is _UNSET:
        from onboard.services import subscriptions as subscription_service
        subscription = subscription_service.current_or_expired(tenant_id)

    checks = [
        wallet_reconciles(wallet),
        no_unrated_calls(tenant_id),
        no_failed_payments(tenant_id),
        lapse_consistent(subscription, wallet),
    ]
    return {"ok": all(c["ok"] for c in checks), "checks": checks}
