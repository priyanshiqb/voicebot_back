"""Confirming a payment from the browser, for tenants who already have an account.

The signup flow has had this since the beginning (`/onboard/verify-payment/`); every
purchase made *after* signup — credits, capacity, renewals, upgrades — had only the
webhook. That left a gap the frontend could not close: Checkout reports success, and
the effect appears whenever Razorpay's server-to-server call lands. Usually seconds,
sometimes not, and never something the browser can wait on.

This is the same endpoint for authenticated tenants. It does not replace the webhook —
it races it, deliberately. Whichever arrives first applies the payment; the other sees
a transaction already marked successful and returns the same answer.
"""

import logging

from django.db import transaction as db_transaction
from drf_spectacular.utils import extend_schema
from rest_framework import status

from onboard.models import Transaction
from onboard.serializers.billing import (
    PaymentVerifiedSerializer,
    VerifyBillingPaymentSerializer,
)
from onboard.services import payment_application, payments
from onboard.views.billing.purchases import _PurchaseBase

logger = logging.getLogger(__name__)

TxnType = Transaction.TransactionTypeChoices
TxnStatus = Transaction.TransactionStatusChoices


@extend_schema(
    tags=["Billing - Purchases"],
    summary="Confirm a payment from the browser",
    description=(
        "Call this when Razorpay Checkout reports success, with the three values it "
        "hands back. The signature is verified and the purchase applied immediately, "
        "so the tenant sees their credits or capacity without waiting for the "
        "webhook.\n\n"
        "**Safe to call even if the webhook got there first** — it returns 200 with "
        "the same body rather than applying anything twice. Equally, not calling it "
        "loses nothing: the webhook still applies the payment on its own."
    ),
    request=VerifyBillingPaymentSerializer,
    responses={200: PaymentVerifiedSerializer},
)
class VerifyPaymentView(_PurchaseBase):
    """Apply a payment the browser has just completed.

    **Idempotent by row lock.** The transaction is locked before its status is read,
    so a webhook arriving in the same instant blocks until this commits and then sees
    `SUCCESSFUL`. Underneath that, credit grants are keyed on the order id, so even a
    bypassed outer guard deposits once.
    """

    serializer_class = VerifyBillingPaymentSerializer

    def post(self, request):
        payload = VerifyBillingPaymentSerializer(data=request.data)
        if not payload.is_valid():
            return self.error(
                message="Validation failed",
                errors=payload.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        tenant = self.get_tenant()
        order_id = payload.validated_data["razorpay_order_id"]
        payment_id = payload.validated_data["razorpay_payment_id"]
        signature = payload.validated_data["razorpay_signature"]

        with db_transaction.atomic():
            transaction = (
                Transaction.objects.select_for_update()
                .filter(razorpay_order_id=order_id)
                .first()
            )

            # Ownership and existence collapse into one answer on purpose. Telling a
            # caller that an order exists but is not theirs is an enumeration oracle
            # over every order id in the system.
            if transaction is None or transaction.tenant_id != tenant.id:
                return self.error(
                    message="No such order for this tenant.",
                    errors=["TRANSACTION_NOT_FOUND"],
                    status_code=status.HTTP_404_NOT_FOUND,
                )

            if transaction.transaction_type == TxnType.ONBOARDING:
                # A signup payment belongs to a TempUser, not a tenant, and applying
                # it here would try to promote a registrant that this tenant is not.
                return self.error(
                    message=(
                        "Signup payments are confirmed through the onboarding flow, "
                        "not here."
                    ),
                    errors=["WRONG_ENDPOINT"],
                    status_code=status.HTTP_409_CONFLICT,
                )

            already_applied = transaction.status == TxnStatus.SUCCESSFUL

            if not already_applied:
                if transaction.status == TxnStatus.CANCELED:
                    # The order expired and was swept before the browser came back.
                    # The money, if any was taken, needs a human — refusing here is
                    # better than applying a purchase against a dead order.
                    logger.error(
                        "order %s was canceled but the browser reports payment %s",
                        order_id,
                        payment_id,
                    )
                    return self.error(
                        message=(
                            "This order expired before the payment was confirmed. "
                            "Contact support with the order id if you were charged."
                        ),
                        errors=["ORDER_EXPIRED"],
                        data={"razorpay_order_id": order_id},
                        status_code=status.HTTP_409_CONFLICT,
                    )

                if not payments.verify_payment_signature(
                    order_id, payment_id, signature
                ):
                    transaction.status = TxnStatus.FAILED
                    transaction.save(update_fields=["status", "updated_at"])
                    return self.error(
                        message="Payment signature verification failed.",
                        errors=["SIGNATURE_VERIFICATION_FAILED"],
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )

                # A valid signature proves Razorpay issued this payment against
                # this order. It does not prove the payment was *captured* rather
                # than merely authorised, and it says nothing about the amount --
                # the HMAC covers order_id|payment_id and nothing else. Neither
                # fact exists client-side, so the gateway is the only source.
                entity = payments.fetch_payment(payment_id)
                error = self._reject_if_gateway_disagrees(
                    transaction, entity, order_id, payment_id
                )
                if error:
                    return error

                paid = entity.get("amount") if entity else None
                if payment_application.check_amount(transaction, paid) == "underpaid":
                    transaction.razorpay_payment_id = payment_id
                    transaction.status = TxnStatus.UNDERPAID
                    transaction.save(update_fields=[
                        "razorpay_payment_id", "status", "updated_at",
                    ])
                    return self.error(
                        message=(
                            "The amount received does not match this order. Nothing "
                            "has been applied — contact support with the order id."
                        ),
                        errors=["AMOUNT_MISMATCH"],
                        data={"razorpay_order_id": order_id},
                        status_code=status.HTTP_409_CONFLICT,
                    )

                transaction.razorpay_payment_id = payment_id
                transaction.status = TxnStatus.SUCCESSFUL
                transaction.save(
                    update_fields=[
                        "razorpay_payment_id",
                        "status",
                        "updated_at",
                    ]
                )

                payment_application.apply(transaction)

        return self.success(
            data=self._state_after(tenant, transaction, already_applied),
            message=(
                "Payment already confirmed"
                if already_applied
                else "Payment verified"
            ),
        )

    def _reject_if_gateway_disagrees(self, transaction, entity, order_id, payment_id):
        """``None`` when Razorpay's own record backs the browser's claim.

        Skipped entirely when the gateway is stubbed -- local work and the test suite
        have no real payment to fetch, the same condition under which signatures are
        not checked either.
        """
        if entity is None:
            # Either the gateway is stubbed (local work, test suite) or the fetch
            # failed. Both proceed on the strength of the signature alone.
            #
            # Fail-open is deliberate, and it matters most in development, where
            # Razorpay cannot reach this host and no webhook will ever arrive: this
            # endpoint is the *only* thing that applies a purchase. Refusing because
            # our own outbound call had a blip would mean a tenant who paid gets
            # nothing, with nothing behind us to put it right.
            logger.info(
                "no gateway record for payment %s on order %s; applying on the "
                "signature alone",
                payment_id, order_id,
            )
            return None

        if entity.get("order_id") != order_id:
            logger.error(
                "payment %s belongs to order %s, not %s",
                payment_id, entity.get("order_id"), order_id,
            )
            return self.error(
                message="That payment does not belong to this order.",
                errors=["PAYMENT_ORDER_MISMATCH"],
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        # "authorized" is money held, not taken. Applying a purchase against one
        # means granting credits for a payment that can still be voided.
        if entity.get("status") != "captured":
            logger.warning(
                "payment %s on order %s is %s, not captured",
                payment_id, order_id, entity.get("status"),
            )
            return self.error(
                message=(
                    "This payment has not completed yet. It will be applied "
                    "automatically once it does."
                ),
                errors=["PAYMENT_NOT_CAPTURED"],
                data={"payment_status": entity.get("status")},
                status_code=status.HTTP_409_CONFLICT,
            )

        return None

    def _state_after(self, tenant, transaction, already_applied):
        """What the tenant now has, so the frontend need not poll to find out.

        The whole point of this endpoint is removing a round trip; making the caller
        follow it with `GET /billing/subscription/` would put that round trip back.
        """
        from onboard.services import entitlements as entitlement_service
        from onboard.services import subscriptions as subscription_service
        from wallet.services import wallet as wallet_service

        data = {
            "razorpay_order_id": transaction.razorpay_order_id,
            "transaction_type": transaction.transaction_type,
            "amount": str(transaction.amount),
            "already_applied": already_applied,
            "credits": None,
            "entitlements": None,
        }

        try:
            data["credits"] = wallet_service.balance_for(tenant)
        except Exception:
            logger.warning("could not read balance after %s", transaction.pk,
                           exc_info=True)

        subscription = subscription_service.current_for(tenant)
        if subscription is not None:
            try:
                data["entitlements"] = entitlement_service.summary(
                    tenant.id, subscription, with_usage=True
                )
            except Exception:
                logger.warning("could not read entitlements after %s", transaction.pk,
                               exc_info=True)

        return data
