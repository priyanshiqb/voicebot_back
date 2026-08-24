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

from django.db import transaction as db_transaction
from django.utils import timezone

from onboard.models.subscription import Subscription

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


@db_transaction.atomic
def activate(tenant, plan, *, transaction=None, previous=None, notes=""):
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

    subscription = Subscription.objects.create(
        tenant_id=tenant_id,
        plan=plan,
        status=Status.ACTIVE,
        start_date=timezone.now(),
    )

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


def _close(subscription, *, status=Status.CANCELLED):
    """End a subscription so another can take its place."""
    subscription.status = status
    subscription.canceled_at = timezone.now()
    subscription.save(update_fields=["status", "canceled_at", "updated_at"])
    return subscription
