"""Provisioning a business that paid outside the payment gateway, on negotiated terms.

The one implementation behind both ways an operator can do this: the
``onboard_custom_deal`` management command and the Manual onboarding screen in the
Django admin. Two copies of this sequence is how the paths drift, so neither holds
its own.

It walks the same sequence a real payment does, so the result is an ordinary tenant
with an ordinary subscription that every other part of the system already
understands. The differences are that the order is recorded as already paid and
verified, and that the terms were typed in rather than copied from the plan. See the
command's docstring for why the plan is only a starting point.
"""

import uuid

from django.contrib.auth.hashers import make_password
from django.db import transaction as db_transaction

from onboard.models import BillingAddress, TempUser, Transaction
from onboard.services import subscriptions as subscription_service
from onboard.views.onboarding.base import create_tenant_and_mapping
from user.models.CustomUser import CustomUser

#: Prefix on the placeholder order id. `razorpay_order_id` is unique and non-null, so a
#: manual deal needs a value there; the prefix makes it obvious in support and in the
#: ledger that no gateway was involved.
MANUAL_ORDER_PREFIX = "manual_"

#: What storage falls back to when the operator leaves it blank. Every other
#: allowance falls back to unlimited; storage does not, because "unlimited" there
#: is an open-ended hosting bill rather than a generous seat count.
DEFAULT_STORAGE_GB = 10


def band_label(plan):
    """A plan's minutes band, phrased for a person: "exactly 5,000 minutes",
    "1,000-10,000 minutes"."""
    if plan.is_fixed_quantity:
        return f"exactly {plan.min_minutes:,} minutes"
    low = f"{plan.min_minutes:,}" if plan.min_minutes is not None else "any"
    high = f"{plan.max_minutes:,}" if plan.max_minutes is not None else "any"
    return f"{low}-{high} minutes"


@db_transaction.atomic
def provision(*, registrant, address, plan, payment, terms, entitlements, minutes=None):
    """Create the user, tenant, paid transaction and subscription. Returns the
    ``Subscription``.

    All or nothing: any failure rolls every row back, so a half-onboarded tenant
    cannot exist.

    ``registrant``: business_name, first_name, last_name, email, phone_number,
    password (plain text; hashed here), referral_code, timezone, language.
    ``address``: line1, city, state, zip_code, country, gstin -- or ``None``.
    ``payment``: amount, reference. ``terms`` and ``entitlements`` are the negotiated
    contract, written to the subscription's ``plan_snapshot``, never to the plan.
    """
    # A real TempUser, not a stand-in. `create_tenant_and_mapping` reads it, and going
    # through the same object the paid path uses means this cannot drift away from how
    # a normal signup builds a tenant.
    temp_user = TempUser.objects.create(
        email=registrant["email"],
        phone_number=registrant["phone_number"],
        first_name=registrant["first_name"],
        last_name=registrant["last_name"],
        business_name=registrant["business_name"],
        referral_code=registrant["referral_code"],
        password_hash=make_password(registrant["password"]),
        selected_plan=plan,
        timezone=registrant["timezone"],
        language=registrant["language"],
        is_email_verified=True,
    )

    # The operator's reference goes in the payment id field, which is where a human
    # looks for "what proves this".
    order_id = f"{MANUAL_ORDER_PREFIX}{uuid.uuid4().hex[:20]}"
    txn = Transaction.objects.create(
        razorpay_order_id=order_id,
        razorpay_payment_id=payment["reference"],
        amount=payment["amount"],
        status=Transaction.TransactionStatusChoices.SUCCESSFUL,
        transaction_type=Transaction.TransactionTypeChoices.ONBOARDING,
        temp_user=temp_user,
        plan=plan,
    )

    user = CustomUser(
        email=registrant["email"],
        username=registrant["email"],
        phone_number=registrant["phone_number"],
        first_name=registrant["first_name"],
        last_name=registrant["last_name"],
        password=temp_user.password_hash,
        is_active=True,
        is_verified=True,
        timezone=registrant["timezone"],
        language=registrant["language"],
    )
    user.save()

    tenant = create_tenant_and_mapping(temp_user, user)

    subscription = subscription_service.activate(
        tenant,
        plan,
        minutes=minutes,
        transaction=txn,
        credit_grant=terms["credits"],
        notes=f"Manual enterprise onboarding — {payment['reference']}",
        snapshot_overrides={
            "entitlements": entitlements,
            "allocated_credits": terms["credits"],
            "max_rollover_credits": terms["max_rollover_credits"],
            "low_balance_threshold": terms["low_balance_threshold"],
            "allow_rollover_on_renewal": terms["allow_rollover_on_renewal"],
            "allow_rollover_on_upgrade": terms["allow_rollover_on_upgrade"],
            "allow_rollover_on_downgrade": terms["allow_rollover_on_downgrade"],
            "allow_use_after_expiry": terms["allow_use_after_expiry"],
            "renews_without_payment": terms["renews_without_payment"],
            # What was actually paid, which for a negotiated deal is not the plan's
            # list price. Frozen here so an invoice query does not have to join back
            # to the transaction.
            "total_price": str(payment["amount"]),
            "manual_payment_reference": payment["reference"],
        },
    )

    if address:
        BillingAddress.objects.create(user=user, **address)

    txn.user = user
    txn.tenant = tenant
    txn.subscription = subscription
    txn.temp_user = None
    txn.save(update_fields=[
        "user", "tenant", "subscription", "temp_user", "updated_at",
    ])

    temp_user.is_onboarded = True
    temp_user.save()
    temp_user.soft_delete()

    return subscription
