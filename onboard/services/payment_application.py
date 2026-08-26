"""Turning a paid transaction into the thing the tenant bought.

**One implementation, two callers.** Razorpay tells us a payment succeeded twice: the
browser comes back from Checkout and posts to ``/billing/verify-payment/``, and
Razorpay's server posts to ``/onboard/webhook/``. Either can arrive first, either can
be the only one that arrives, and both must produce exactly the same effect.

Before this module the webhook owned the effect and the browser path owned a
near-copy of it, which is the shape defects grow in: a fix to one side silently
leaves the other applying a stale rule. The dispatch lives here now, and both entry
points call :func:`apply`.

**Idempotency is the caller's job first, and ours second.** Callers must lock the
``Transaction`` row and refuse to call in when it is already ``SUCCESSFUL`` — that is
what stops two concurrent deliveries both applying. Underneath, the credit ledger's
unique ``(entry_type, reference_id)`` catches anything that slips past, so a
redelivery deposits once even if the outer guard is bypassed.

Nothing here raises on a *business* problem. A transaction missing the data its
handler needs is logged loudly and left alone: the payment is real and already
recorded, so refusing it would lose track of money. What is missing is the effect,
and that is what the log line is for.
"""

import logging
from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model

from onboard.models import BillingAddress, Transaction

logger = logging.getLogger(__name__)
User = get_user_model()

TxnType = Transaction.TransactionTypeChoices



#: Paise. Razorpay reports integers and we send integers, so a genuine payment matches
#: exactly. A tolerance here would only ever mask a rounding bug in our own pricing.
AMOUNT_TOLERANCE_PAISE = 0


def expected_paise(transaction):
    """What this order should have been paid, in paise."""
    return int((transaction.amount * 100).to_integral_value())


def check_amount(transaction, paid_paise):
    """Compare what was actually paid against what was owed.

    Returns ``"ok"``, ``"underpaid"``, ``"overpaid"``, or ``"unknown"`` when the
    amount could not be established.

    **Underpaid must not be applied.** Whether that is reachable through Razorpay
    today is beside the point: orders are created with a fixed amount and partial
    payments are not enabled, so a mismatch means something more interesting than
    fraud — a pricing bug of ours, an order edited in the dashboard, or partial
    payments switched on without this code knowing. All three are things we want to
    hear about rather than silently grant.

    **Overpaid is applied.** The tenant paid more than they owed; withholding what
    they bought as well would be the worse of the two mistakes. It is logged so the
    difference can be refunded.
    """
    if paid_paise is None:
        return "unknown"

    expected = expected_paise(transaction)
    delta = paid_paise - expected

    if abs(delta) <= AMOUNT_TOLERANCE_PAISE:
        return "ok"

    if delta < 0:
        logger.error(
            "order %s underpaid: expected %d paise, received %d (short by %d). "
            "NOT applying the purchase.",
            transaction.razorpay_order_id, expected, paid_paise, -delta,
        )
        return "underpaid"

    logger.error(
        "order %s overpaid: expected %d paise, received %d (over by %d). Applying "
        "the purchase; the difference needs refunding.",
        transaction.razorpay_order_id, expected, paid_paise, delta,
    )
    return "overpaid"


def apply(transaction):
    """Apply the effect of ``transaction``, dispatching on its type.

    The caller is responsible for having marked the transaction ``SUCCESSFUL`` under
    a row lock. This does not re-check that: doing so would make the function
    unusable from the onboarding path, where the status is set in the same
    transaction a moment earlier.
    """
    handler = _HANDLERS.get(transaction.transaction_type, _apply_unimplemented)
    return handler(transaction)


def _apply_onboarding(transaction):
    """Promote the pending registrant into a real account.

    Returns early when the TempUser has already been consumed, which is how a
    redelivered webhook behaves when the browser path won the race.
    """
    from onboard.services import subscriptions as subscription_service
    from onboard.views.onboarding.base import create_tenant_and_mapping

    temp_user = transaction.temp_user
    if temp_user is None:
        logger.info(
            "order %s has no pending registrant; onboarding already completed",
            transaction.razorpay_order_id,
        )
        return

    user = User(
        email=temp_user.email,
        username=temp_user.email,
        phone_number=temp_user.phone_number,
        first_name=temp_user.first_name,
        last_name=temp_user.last_name,
        password=temp_user.password_hash,
        is_active=True,
        is_verified=True,
        timezone=temp_user.timezone,
        language=temp_user.language,
    )
    user.save()

    tenant = create_tenant_and_mapping(temp_user, user)
    subscription_service.activate(
        tenant, temp_user.selected_plan, transaction=transaction
    )

    BillingAddress.objects.filter(temp_user=temp_user).update(
        user=user, temp_user=None
    )
    transaction.user = user
    transaction.tenant = tenant
    transaction.temp_user = None
    transaction.save(update_fields=["user", "tenant", "temp_user", "updated_at"])

    temp_user.is_onboarded = True
    temp_user.save()
    temp_user.soft_delete()


def _apply_top_up(transaction):
    """Deposit purchased credits.

    Into the **purchased** bucket, which rollover rules never touch — these were
    bought with cash, and expiring them at a cycle boundary would be taking money for
    nothing. The grant is keyed on the order id, so a redelivery credits once.
    """
    from wallet.models import CreditLedger
    from wallet.services import wallet as wallet_service

    credits = transaction.credits_purchased or 0
    if credits <= 0:
        logger.error(
            "top-up order %s carries no credit quantity; nothing to deposit",
            transaction.razorpay_order_id,
        )
        return

    entry = wallet_service.grant(
        transaction.tenant_id,
        entry_type=CreditLedger.EntryType.MANUAL_TOP_UP,
        amount=credits,
        bucket=CreditLedger.Bucket.PURCHASED,
        reference_id=transaction.razorpay_order_id,
        subscription=transaction.subscription,
        notes="Manual credit top-up",
    )

    # Only on a real grant — a redelivered webhook returns None, and telling a tenant
    # twice that their credits arrived is worse than not telling them.
    if entry is not None:
        from onboard.services import billing_notifications

        try:
            billing_notifications.notify_credits_added(
                transaction.tenant_id,
                credits=credits,
                reason="Your credit top-up has been applied",
            )
        except Exception:
            logger.warning("top-up notification failed", exc_info=True)


def _apply_addon(transaction):
    """Grant the extra capacity that was paid for.

    Quantities come from the breakdown frozen when the order was created, not from a
    fresh calculation: the tenant paid for what they were quoted, and the catalogue
    may have moved since.
    """
    from onboard.services import entitlements as entitlement_service

    breakdown = transaction.amount_breakdown or {}
    resource = breakdown.get("resource")
    units = int(breakdown.get("additional_units") or 0)

    if not resource or units <= 0:
        logger.error(
            "add-on order %s has no usable breakdown; capacity NOT granted",
            transaction.razorpay_order_id,
        )
        return

    entitlement_service.apply_addon(
        tenant_id=transaction.tenant_id,
        subscription=transaction.subscription,
        resource=resource,
        additional_units=units,
        notes=f"Purchased via {transaction.razorpay_order_id}",
    )


def _apply_renewal(transaction):
    """Extend the cycle and fund it."""
    from onboard.services import subscriptions as subscription_service

    if transaction.subscription is None:
        logger.error(
            "renewal order %s has no subscription attached; cycle NOT extended",
            transaction.razorpay_order_id,
        )
        return

    subscription_service.renew(transaction.subscription, transaction=transaction)


def _apply_upgrade(transaction):
    """Move the tenant to the plan they just paid the difference for.

    Any scheduled downgrade is dropped first: one set weeks ago would otherwise fire
    later and silently undo the upgrade they have just bought.
    """
    from onboard.services import subscriptions as subscription_service

    subscription = transaction.subscription
    if subscription is None or transaction.target_plan is None:
        logger.error(
            "upgrade order %s is missing its subscription or target plan; plan NOT "
            "changed",
            transaction.razorpay_order_id,
        )
        return

    # Read back from the quote frozen at order time, not recomputed. The tenant
    # paid on the strength of a specific number of bonus days; time has passed
    # since, and recomputing would silently hand them fewer than they bought.
    breakdown = transaction.amount_breakdown or {}
    try:
        bonus_days = Decimal(str(breakdown.get("bonus_days") or "0"))
    except (InvalidOperation, TypeError):
        logger.warning(
            "upgrade order %s has an unreadable bonus_days (%r); extending by nothing",
            transaction.razorpay_order_id, breakdown.get("bonus_days"),
        )
        bonus_days = Decimal("0")

    subscription_service.cancel_scheduled_change(subscription)
    subscription_service.apply_plan_change(
        subscription,
        transaction.target_plan,
        transaction=transaction,
        reason="upgrade",
        bonus_days=bonus_days,
    )


def _apply_unimplemented(transaction):
    """A payment type whose handler has not been built yet.

    Loud rather than silent, and — importantly — **not** routed to onboarding. Before
    this dispatch existed, every captured payment ran the signup path, so a top-up
    would have tried to promote a TempUser that does not exist. The transaction is
    still marked successful, so no money is lost track of; what is missing is the
    credits or plan change it should have produced.
    """
    logger.error(
        "no handler for %s payment on order %s — payment recorded, effect NOT applied",
        transaction.transaction_type,
        transaction.razorpay_order_id,
    )


_HANDLERS = {
    TxnType.ONBOARDING: _apply_onboarding,
    TxnType.TOP_UP: _apply_top_up,
    TxnType.ADDON: _apply_addon,
    TxnType.RENEWAL: _apply_renewal,
    TxnType.UPGRADE: _apply_upgrade,
}
