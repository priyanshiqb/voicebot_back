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
