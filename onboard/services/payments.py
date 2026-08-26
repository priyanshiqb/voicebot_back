"""Creating Razorpay orders.

One helper, so the gateway stub lives in a single place. Three views used to inline
this, each with its own idea of when to skip the gateway.
"""

import logging
import sys
import uuid
from decimal import Decimal

from django.conf import settings

logger = logging.getLogger(__name__)

#: Razorpay rejects orders below Rs 1.00. Anything smaller has to be applied without
#: a payment rather than sent to a gateway that will refuse it.
MINIMUM_CHARGE = Decimal("1.00")


def gateway_is_live():
    """Whether orders should really be created at Razorpay.

    The same switch that decides whether callbacks are signature-checked: both mean
    "this environment is talking to real Razorpay".
    """
    is_testing = getattr(settings, "TESTING", False) or "test" in sys.argv
    return not (is_testing or settings.PAYMENTS_ALLOW_UNSIGNED)


def create_order(amount, *, receipt, notes=None):
    """Create a Razorpay order for a rupee ``amount``. Returns the order id.

    Amounts go to Razorpay in paise, converted here rather than trusting each caller
    to remember the multiply-by-100.
    """
    from onboard.views.onboarding.base import razorpay_client

    amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    if amount < MINIMUM_CHARGE:
        raise ValueError(
            f"{amount} is below the {MINIMUM_CHARGE} gateway minimum; apply the "
            f"change without a payment rather than creating an order that will be "
            f"refused."
        )

    if not gateway_is_live():
        return f"order_{uuid.uuid4().hex[:14]}"

    order = razorpay_client.order.create({
        "amount": int(amount * 100),
        "currency": "INR",
        "receipt": receipt[:40],
        "payment_capture": 1,
        "notes": notes or {},
    })
    return order["id"]


def verify_payment_signature(order_id, payment_id, signature):
    """Whether Razorpay really issued this payment for this order.

    The browser hands back three values after Checkout and any of them can be forged,
    so the signature is the only thing that makes a client-reported success
    trustworthy. Returns ``True``/``False`` rather than raising: the caller has a
    transaction to mark FAILED either way.

    **Skipped when the gateway is not live**, on the same switch that decides whether
    orders are really created. Local work and the test suite have no real signature to
    check, and gating on one would make every payment path untestable.
    """
    if not gateway_is_live():
        return True

    import razorpay

    from onboard.views.onboarding.base import razorpay_client

    try:
        razorpay_client.utility.verify_payment_signature({
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        })
        return True
    except razorpay.errors.SignatureVerificationError:
        logger.warning("bad payment signature for order %s", order_id)
        return False


def fetch_payment(payment_id):
    """Ask Razorpay what actually happened, or ``None`` when the gateway is stubbed.

    The browser hands back three strings, and the payment signature is an HMAC over
    ``order_id|payment_id`` only. It proves Razorpay issued *a* payment against *that
    order* — it says nothing about how much was paid, and nothing about whether the
    payment was ever captured rather than merely authorised. Neither fact exists
    client-side to be checked, so the only honest source is the gateway.

    Returns the payment entity as Razorpay reports it: ``amount`` in paise,
    ``status``, ``order_id``, and the rest.
    """
    if not gateway_is_live():
        return None

    from onboard.views.onboarding.base import razorpay_client

    try:
        return razorpay_client.payment.fetch(payment_id)
    except Exception:
        logger.exception("could not fetch payment %s from Razorpay", payment_id)
        return None
