"""Subscription lifecycle.

The one place a subscription comes into existence. Three signup paths used to do it
inline — the free-plan checkout, the browser-confirmed payment, and the Razorpay
webhook — and every one of them created a bare row with no end date, no credits and
no rate card. Pasting credit allocation into three views would have guaranteed they
drift apart the first time one of them changed.

**Order is load-bearing.** Stamping the wallet with the plan's rate card must happen
before any credits are granted: a grant that lands first is credited while the wallet
still carries the previous plan's prices, and a call arriving between the two is rated
at the wrong tier.
"""

import logging

from django.db import IntegrityError
from django.db import transaction as db_transaction
from django.utils import timezone

from onboard.models.subscription import Subscription
from onboard.services import billing_notifications

logger = logging.getLogger(__name__)

Status = Subscription.SubscriptionStatusChoices


def current_for(tenant):
    """The tenant's live subscription, or ``None``.

    A partial unique constraint guarantees at most one, so ``.first()`` is not a
    silent choice between candidates.
    """
    tenant_id = getattr(tenant, "id", tenant)
    return Subscription.objects.filter(
        tenant_id=tenant_id, status=Status.ACTIVE
    ).first()


def current_or_expired(tenant):
    """The live subscription, or the most recent lapsed one.

    A tenant whose cycle ended still has a relationship with us, and almost every
    question they might ask — what did I have, what does it cost to come back, what
    happened to my credits — needs that record. Returning ``None`` the moment a cycle
    ends means the dashboard cannot even tell them why they are blocked.

    Cancelled subscriptions are excluded: those were superseded by a plan change, so
    the row that replaced them is the one that matters.
    """
    tenant_id = getattr(tenant, "id", tenant)
    live = current_for(tenant_id)
    if live is not None:
        return live
    return (
        Subscription.objects.filter(tenant_id=tenant_id)
        .exclude(status=Status.CANCELLED)
        .order_by("-end_date")
        .first()
    )


@db_transaction.atomic
def activate(tenant, plan, *, transaction=None, previous=None, notes="",
             bonus_days=None):
    """Put a tenant on a plan: subscription, rate card, and the cycle's credits.

    Returns the live ``Subscription``.

    **Idempotent for the same plan.** The browser-confirmed payment and the Razorpay
    webhook both complete onboarding, and both can win — so a second call for a plan
    the tenant is already on returns the existing subscription rather than tripping
    the one-active-per-tenant constraint. The credit grant is separately idempotent on
    the payment's order id, so credits cannot be issued twice either way.

    ``previous`` is for plan changes: the caller has already decided what to forfeit,
    and passes the outgoing subscription so the chain stays navigable.
    """
    from wallet.services import wallet as wallet_service
    from wallet.models import CreditLedger

    tenant_id = getattr(tenant, "id", tenant)
    existing = current_for(tenant_id)

    if existing is not None:
        if existing.plan_id == plan.id and previous is None:
            logger.info(
                "activate() called again for tenant %s on %s; returning the existing "
                "subscription", tenant_id, plan.code,
            )
            _ensure_wallet_funded(existing, plan, transaction, wallet_service, CreditLedger)
            return existing

        # A different plan means this is a change, not a re-entry. Close the old row
        # before inserting the new one: the unique constraint is checked per
        # statement, not deferred, so insert-then-close fails.
        _close(existing)
        previous = previous or existing

    try:
        # Savepointed: an IntegrityError poisons the surrounding transaction, and
        # this method is atomic. Without the savepoint the recovery below could not
        # run any queries.
        with db_transaction.atomic():
            started = timezone.now()
            subscription = Subscription.objects.create(
                tenant_id=tenant_id,
                plan=plan,
                status=Status.ACTIVE,
                start_date=started,
                # An upgrade converts the unused value of the old plan into days on
                # the new one, so this cycle runs long. Computed here rather than
                # left to `save()`, which only ever fills a blank end_date.
                end_date=_end_date_with_bonus(
                    Subscription(plan=plan, start_date=started), bonus_days
                ),
            )
    except IntegrityError:
        # Another worker created the tenant's subscription between the check above
        # and this insert. `uniq_active_subscription_per_tenant` is what caught it,
        # and that is the constraint working — but the caller has paid for something
        # and must not receive an error because they lost a race they did not know
        # they were in.
        #
        # The webhook and the browser-confirmed payment both serialise on the
        # transaction row, so this is narrow. It is reachable when two *different*
        # orders complete for one tenant at once.
        existing = current_for(tenant_id)
        if existing is None or existing.plan_id != plan.id:
            raise
        logger.info(
            "activate() lost a race for tenant %s; adopting the subscription the "
            "winner created", tenant_id,
        )
        _ensure_wallet_funded(existing, plan, transaction, wallet_service, CreditLedger)
        return existing

    if previous is not None:
        previous.superseded_by = subscription
        previous.save(update_fields=["superseded_by", "updated_at"])

    _ensure_wallet_funded(subscription, plan, transaction, wallet_service, CreditLedger, notes=notes)
    return subscription


def _ensure_wallet_funded(subscription, plan, transaction, wallet_service, CreditLedger, notes=""):
    """Stamp the rate card, then grant the cycle's credits. In that order.

    Both steps are safe to repeat: stamping is a write of the same values, and the
    grant is keyed on the payment reference.

    A failure here does not roll back the subscription. The tenant has paid and the
    subscription is real; leaving them with an unfunded wallet is a support ticket,
    while refusing the whole activation is a lost customer. The reconciliation and
    unrated-call jobs are what surface the gap.
    """
    try:
        wallet_service.set_plan_context(subscription.tenant_id, plan)
    except Exception:
        logger.exception(
            "could not stamp rate card for tenant %s on %s",
            subscription.tenant_id, plan.code,
        )
        return

    allocation = plan.allocated_credits or 0
    if allocation <= 0:
        return

    reference = (
        transaction.razorpay_order_id
        if transaction is not None
        else f"subscription:{subscription.pk}"
    )

    try:
        wallet_service.grant(
            subscription.tenant_id,
            entry_type=CreditLedger.EntryType.CYCLE_ALLOCATION,
            amount=allocation,
            reference_id=reference,
            subscription=subscription,
            notes=notes,
        )
    except Exception:
        logger.exception(
            "could not grant %s credits to tenant %s (subscription %s)",
            allocation, subscription.tenant_id, subscription.pk,
        )


def _announce(send):
    """Fire a notification without letting it affect the outcome.

    A lifecycle change that succeeded must not be reported as failed because a push
    could not be queued, and it must not be rolled back either.
    """
    try:
        send()
    except Exception:
        logger.warning("billing notification failed", exc_info=True)


def _close(subscription, *, status=Status.CANCELLED):
    """End a subscription so another can take its place."""
    subscription.status = status
    subscription.canceled_at = timezone.now()
    subscription.save(update_fields=["status", "canceled_at", "updated_at"])
    return subscription


# --------------------------------------------------------------------------- #
# Rollover
# --------------------------------------------------------------------------- #

#: Which of the plan's flags governs each transition.
ROLLOVER_FLAGS = {
    "renewal": "allow_rollover_on_renewal",
    "upgrade": "allow_rollover_on_upgrade",
    "downgrade": "allow_rollover_on_downgrade",
}


def rollover_amount(subscription, *, reason):
    """How many plan-granted credits survive this transition.

    Reads the **outgoing** subscription's frozen rules, not the incoming plan's: the
    terms that govern a balance are the ones the tenant agreed to when they were
    granted it.

    Only the allocated bucket is ever at stake. Purchased credits were bought with
    cash and always carry — expiring them would be taking money for nothing.

    Returns ``(carried, forfeited, reason_code)`` where ``reason_code`` distinguishes
    "your plan does not roll over" from "you hit the cap", because those are two
    different conversations with a customer.
    """
    from wallet.services import wallet as wallet_service

    rules = subscription.rules
    wallet = wallet_service.for_tenant(subscription.tenant_id)
    held = wallet.allocated_balance

    if not rules.get(ROLLOVER_FLAGS[reason], False):
        return 0, held, "rollover_forfeit"

    cap = rules.get("max_rollover_credits")
    if cap is not None and held > cap:
        return cap, held - cap, "rollover_cap_forfeit"

    return held, 0, None


def _end_date_with_bonus(draft, bonus_days):
    """One cycle from ``start_date``, plus any days bought with unused value.

    ``bonus_days`` is fractional -- a partial day is real money and rounding it away
    would quietly keep the difference. Converted to seconds so nothing is lost.
    """
    from datetime import timedelta
    from decimal import Decimal

    draft.generate_plan_snapshot()
    end = draft.derive_end_date()
    if not bonus_days:
        return end
    return end + timedelta(seconds=int(Decimal(str(bonus_days)) * 86400))


def _apply_rollover(subscription, *, reason):
    """Burn whatever the rollover rules do not let the tenant keep."""
    from wallet.models import CreditLedger
    from wallet.services import wallet as wallet_service

    carried, forfeited, code = rollover_amount(subscription, reason=reason)
    if forfeited <= 0:
        return carried

    entry_type = {
        "rollover_forfeit": CreditLedger.EntryType.ROLLOVER_FORFEIT,
        "rollover_cap_forfeit": CreditLedger.EntryType.ROLLOVER_CAP_FORFEIT,
    }[code]

    wallet_service.forfeit(
        subscription.tenant_id,
        entry_type=entry_type,
        amount=forfeited,
        bucket=CreditLedger.Bucket.ALLOCATED,
        # Suffixed by reason so a tenant who renews, upgrades and renews again does
        # not collide on the same subscription id.
        reference_id=f"{subscription.pk}:{reason}",
        subscription=subscription,
        notes=f"{reason} rollover: kept {carried}, forfeited {forfeited}",
    )
    return carried


# --------------------------------------------------------------------------- #
# Renewal
# --------------------------------------------------------------------------- #

@db_transaction.atomic
def renew(subscription, *, transaction=None):
    """Extend a subscription by one cycle and fund the new one.

    The **same row**, not a new one — a renewal is a continuation, and creating a
    second subscription would make the history of a long-lived customer a chain of
    near-identical rows.

    Terms are re-frozen from the live plan, because a renewal is a future cycle: this
    is where a price or rule change legitimately reaches an existing customer.
    """
    from wallet.models import CreditLedger
    from wallet.services import wallet as wallet_service

    subscription = Subscription.objects.select_for_update().get(pk=subscription.pk)
    plan = subscription.plan

    # Burn what does not carry, before the new allocation lands — otherwise the
    # forfeit would eat into credits the tenant just paid for.
    _apply_rollover(subscription, reason="renewal")

    # A lapsed subscription being paid for is a renewal, not a new sale.
    was_expired = subscription.status == Status.EXPIRED

    start = subscription.end_date or timezone.now()
    # A renewal paid long after expiry starts now rather than back-dating a cycle the
    # tenant could not use.
    if start < timezone.now():
        start = timezone.now()

    subscription.start_date = start
    subscription.end_date = start + subscription.cycle_delta()
    subscription.status = Status.ACTIVE
    subscription.canceled_at = None
    subscription.generate_plan_snapshot()
    subscription.save()

    wallet_service.set_plan_context(subscription.tenant_id, plan)
    if was_expired:
        wallet_service.unfreeze(subscription.tenant_id)

    allocation = plan.allocated_credits or 0
    if allocation > 0:
        reference = (
            transaction.razorpay_order_id
            if transaction is not None
            else f"renewal:{subscription.pk}:{subscription.start_date.isoformat()}"
        )
        wallet_service.grant(
            subscription.tenant_id,
            entry_type=CreditLedger.EntryType.CYCLE_ALLOCATION,
            amount=allocation,
            reference_id=reference,
            subscription=subscription,
            notes="Renewal allocation",
        )

    return subscription


# --------------------------------------------------------------------------- #
# Plan changes
# --------------------------------------------------------------------------- #

@db_transaction.atomic
def apply_plan_change(subscription, new_plan, *, transaction=None, reason="upgrade",
                      bonus_days=None):
    """Move a tenant to a different plan, now.

    Used for an upgrade once its prorated order is paid, and by the scheduler when a
    downgrade's cycle finally ends.

    The wallet never moves. Closing one subscription and opening another is a change
    of *terms*, not of balance — so all that happens to the credits is that whatever
    the old plan's rules do not let the tenant keep is forfeited.
    """
    subscription = Subscription.objects.select_for_update().get(pk=subscription.pk)

    _apply_rollover(subscription, reason=reason)
    _close(subscription)

    new_subscription = activate(
        subscription.tenant_id,
        new_plan,
        transaction=transaction,
        previous=subscription,
        notes=f"{reason} from {subscription.plan.code}",
        bonus_days=bonus_days,
    )
    _announce(lambda: billing_notifications.notify_plan_changed(
        new_subscription, previous_plan_code=subscription.plan.code
    ))
    return new_subscription


@db_transaction.atomic
def schedule_plan_change(subscription, new_plan):
    """Record a change to take effect when the current cycle ends.

    A downgrade is stored, never applied early: the tenant has paid for the cycle they
    are in, and ending it would mean refunding cash or taking away something they
    bought. Storing it also means no second subscription exists ahead of time, so the
    one-active-per-tenant constraint is never approached.
    """
    subscription = Subscription.objects.select_for_update().get(pk=subscription.pk)
    subscription.scheduled_plan = new_plan
    subscription.scheduled_change_at = subscription.end_date
    subscription.save(
        update_fields=["scheduled_plan", "scheduled_change_at", "updated_at"]
    )
    return subscription


@db_transaction.atomic
def cancel_scheduled_change(subscription):
    """Drop a pending change.

    Called explicitly by a tenant who changed their mind, and implicitly whenever an
    upgrade is applied — otherwise a downgrade scheduled last week fires later and
    silently undoes a change they just paid for.
    """
    subscription = Subscription.objects.select_for_update().get(pk=subscription.pk)
    if subscription.scheduled_plan_id is None:
        return subscription
    subscription.scheduled_plan = None
    subscription.scheduled_change_at = None
    subscription.save(
        update_fields=["scheduled_plan", "scheduled_change_at", "updated_at"]
    )
    return subscription


# --------------------------------------------------------------------------- #
# Expiry
# --------------------------------------------------------------------------- #

@db_transaction.atomic
def expire(subscription):
    """End a cycle that was not paid for.

    **Purchased credits are frozen, not confiscated.** An earlier draft forfeited the
    whole balance; that would take cash a tenant paid for a top-up and give nothing
    back. Freezing preserves them — unspendable while lapsed, and spendable again the
    moment a renewal clears — while the plan-granted half is forfeited because that is
    what "your plan ended" means.

    A plan with ``allow_use_after_expiry`` skips both: the subscription lapses but the
    wallet stays live, which is how pay-as-you-go behaves.
    """
    from wallet.models import CreditLedger
    from wallet.services import wallet as wallet_service

    subscription = Subscription.objects.select_for_update().get(pk=subscription.pk)
    subscription.status = Status.EXPIRED
    subscription.save(update_fields=["status", "updated_at"])

    if subscription.rules.get("allow_use_after_expiry", False):
        logger.info(
            "subscription %s expired; wallet left spendable by plan rule",
            subscription.pk,
        )
        return subscription

    wallet_service.forfeit(
        subscription.tenant_id,
        entry_type=CreditLedger.EntryType.EXPIRY_FORFEIT,
        bucket=CreditLedger.Bucket.ALLOCATED,
        reference_id=f"{subscription.pk}:expiry",
        subscription=subscription,
        notes="Plan credits forfeited on expiry",
    )
    wallet_service.freeze(subscription.tenant_id, reason="subscription_expired")
    _announce(lambda: billing_notifications.notify_expired(subscription))
    return subscription
