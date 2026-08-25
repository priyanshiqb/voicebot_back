import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction as db_transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status

from onboard.models import Transaction
from onboard.serializers.billing import (
    AddonPurchaseSerializer,
    OrderCreatedSerializer,
    QuoteSerializer,
    TopUpPurchaseSerializer,
)
from onboard.services import payments, pricing
from onboard.services import subscriptions as subscription_service
from tenant.common.views.tenant_base_api_view import TenantBaseAPIView

logger = logging.getLogger(__name__)

TxnType = Transaction.TransactionTypeChoices
TxnStatus = Transaction.TransactionStatusChoices

#: How long an unpaid order stays valid. Without a TTL a single abandoned checkout
#: blocks the tenant from starting another purchase indefinitely.
ORDER_TTL = timedelta(minutes=30)


class _PurchaseBase(TenantBaseAPIView):
    """Shared plumbing for "quote it, then charge for it" endpoints."""

    def _subscription_or_error(self):
        """The live subscription, or an error response.

        For anything that only makes sense on a running plan — buying credits, buying
        capacity. Renting capacity for a cycle that is not running has no cycle to
        belong to.
        """
        subscription = subscription_service.current_for(self.get_tenant())
        if subscription is None:
            return None, self.error(
                message="No active subscription. Start or renew a plan first.",
                errors=["NO_ACTIVE_SUBSCRIPTION"],
                status_code=status.HTTP_409_CONFLICT,
            )
        return subscription, None

    def _subscription_or_expired_or_error(self):
        """The live subscription, or the most recent lapsed one.

        For reading state and for coming back. A tenant whose plan ended still needs
        to see what they had, what it would cost to return, and what happened to their
        credits — refusing all of that is how someone ends up unable to give us money.
        """
        subscription = subscription_service.current_or_expired(self.get_tenant())
        if subscription is None:
            return None, self.error(
                message="No subscription found. Choose a plan to get started.",
                errors=["NO_SUBSCRIPTION"],
                status_code=status.HTTP_409_CONFLICT,
            )
        return subscription, None

    def _blocking_order(self, tenant):
        """An unpaid order that must be settled or expire before another is made.

        One open order at a time. Without this a tenant can pay two upgrade orders
        and have only one applied, which is the worst outcome available: money taken,
        effect missing.
        """
        Transaction.objects.filter(
            tenant=tenant,
            status=TxnStatus.PENDING,
            expires_at__lt=timezone.now(),
        ).update(status=TxnStatus.CANCELED)

        return (
            Transaction.objects.filter(tenant=tenant, status=TxnStatus.PENDING)
            .exclude(transaction_type=TxnType.ONBOARDING)
            .first()
        )

    def _create_order(self, *, tenant, subscription, quote, transaction_type, receipt,
                      **extra):
        """Persist the intent, then ask the gateway for an order id.

        The transaction is written first so a gateway timeout leaves a PENDING row to
        reconcile against, rather than a payment nobody can attribute.
        """
        with db_transaction.atomic():
            transaction = Transaction.objects.create(
                razorpay_order_id=f"pending_{timezone.now().timestamp()}",
                amount=quote.total,
                status=TxnStatus.PENDING,
                transaction_type=transaction_type,
                tenant=tenant,
                subscription=subscription,
                plan=subscription.plan,
                amount_breakdown=quote.as_breakdown(),
                expires_at=timezone.now() + ORDER_TTL,
                **extra,
            )
            try:
                order_id = payments.create_order(
                    quote.total,
                    receipt=receipt,
                    notes={"tenant": str(tenant.id), "type": transaction_type},
                )
            except Exception:
                logger.exception("gateway refused an order for tenant %s", tenant.id)
                raise

            transaction.razorpay_order_id = order_id
            transaction.save(update_fields=["razorpay_order_id", "updated_at"])

        return transaction

    def _order_response(self, transaction, quote, message):
        return self.success(
            data={
                "razorpay_order_id": transaction.razorpay_order_id,
                "amount": int(quote.total * 100),
                "amount_display": str(quote.total),
                "currency": "INR",
                "razorpay_key_id": settings.RAZORPAY_KEY_ID,
                "expires_at": transaction.expires_at,
                "breakdown": quote.as_breakdown(),
            },
            message=message,
        )


@extend_schema(
    tags=["Billing - Purchases"],
    summary="Quote a credit top-up",
    description="What N additional credits would cost. Nothing is created or charged.",
    request=TopUpPurchaseSerializer,
    responses={200: QuoteSerializer},
)
class TopUpQuoteView(_PurchaseBase):
    serializer_class = TopUpPurchaseSerializer

    def post(self, request):
        payload = TopUpPurchaseSerializer(data=request.data)
        if not payload.is_valid():
            return self.error(message="Validation failed", errors=payload.errors,
                              status_code=status.HTTP_400_BAD_REQUEST)

        subscription, error = self._subscription_or_error()
        if error:
            return error

        try:
            quote = pricing.quote_top_up(subscription, payload.validated_data["credits"])
        except pricing.PricingError as exc:
            return self.error(message=str(exc), errors=["TOP_UPS_UNAVAILABLE"],
                              status_code=status.HTTP_409_CONFLICT)

        return self.success(data=quote.as_breakdown(), message="Quote calculated")


@extend_schema(
    tags=["Billing - Purchases"],
    summary="Buy additional credits",
    description=(
        "Creates a Razorpay order for N credits. The credits are added when the "
        "payment clears, not here — so an abandoned checkout costs nothing."
    ),
    request=TopUpPurchaseSerializer,
    responses={200: OrderCreatedSerializer},
)
class TopUpOrderView(_PurchaseBase):
    serializer_class = TopUpPurchaseSerializer

    def post(self, request):
        payload = TopUpPurchaseSerializer(data=request.data)
        if not payload.is_valid():
            return self.error(message="Validation failed", errors=payload.errors,
                              status_code=status.HTTP_400_BAD_REQUEST)

        tenant = self.get_tenant()
        subscription, error = self._subscription_or_error()
        if error:
            return error

        blocking = self._blocking_order(tenant)
        if blocking is not None:
            return self.error(
                message=(
                    "An unpaid order is already open. Complete or cancel it before "
                    "starting another."
                ),
                errors=["PENDING_ORDER_EXISTS"],
                data={"razorpay_order_id": blocking.razorpay_order_id},
                status_code=status.HTTP_409_CONFLICT,
            )

        credits = payload.validated_data["credits"]
        try:
            quote = pricing.quote_top_up(subscription, credits)
        except pricing.PricingError as exc:
            return self.error(message=str(exc), errors=["TOP_UPS_UNAVAILABLE"],
                              status_code=status.HTTP_409_CONFLICT)

        try:
            transaction = self._create_order(
                tenant=tenant, subscription=subscription, quote=quote,
                transaction_type=TxnType.TOP_UP,
                receipt=f"topup_{credits}",
                credits_purchased=credits,
            )
        except Exception:
            return self.error(message="Could not reach the payment gateway.",
                              errors=["GATEWAY_ERROR"],
                              status_code=status.HTTP_502_BAD_GATEWAY)

        return self._order_response(transaction, quote, "Top-up order created")


@extend_schema(
    tags=["Billing - Purchases"],
    summary="Quote additional capacity",
    description=(
        "What extra agents, storage or other capacity would cost. Returns both the "
        "prorated charge for the rest of this cycle and the recurring amount it adds "
        "to every renewal."
    ),
    request=AddonPurchaseSerializer,
    responses={200: QuoteSerializer},
)
class AddonQuoteView(_PurchaseBase):
    serializer_class = AddonPurchaseSerializer

    def post(self, request):
        payload = AddonPurchaseSerializer(data=request.data)
        if not payload.is_valid():
            return self.error(message="Validation failed", errors=payload.errors,
                              status_code=status.HTTP_400_BAD_REQUEST)

        subscription, error = self._subscription_or_error()
        if error:
            return error

        try:
            quote = pricing.quote_addon(
                subscription,
                payload.validated_data["resource"],
                payload.validated_data["quantity"],
            )
        except pricing.PricingError as exc:
            return self.error(message=str(exc), errors=["ADDON_UNAVAILABLE"],
                              status_code=status.HTTP_409_CONFLICT)

        return self.success(data=quote.as_breakdown(), message="Quote calculated")


@extend_schema(
    tags=["Billing - Purchases"],
    summary="Buy additional capacity",
    description=(
        "Creates a Razorpay order for the prorated remainder of this cycle. The "
        "capacity is granted when the payment clears, and re-charged in full at every "
        "renewal until it is removed."
    ),
    request=AddonPurchaseSerializer,
    responses={200: OrderCreatedSerializer},
)
class AddonOrderView(_PurchaseBase):
    serializer_class = AddonPurchaseSerializer

    def post(self, request):
        payload = AddonPurchaseSerializer(data=request.data)
        if not payload.is_valid():
            return self.error(message="Validation failed", errors=payload.errors,
                              status_code=status.HTTP_400_BAD_REQUEST)

        tenant = self.get_tenant()
        subscription, error = self._subscription_or_error()
        if error:
            return error

        blocking = self._blocking_order(tenant)
        if blocking is not None:
            return self.error(
                message=(
                    "An unpaid order is already open. Complete or cancel it before "
                    "starting another."
                ),
                errors=["PENDING_ORDER_EXISTS"],
                data={"razorpay_order_id": blocking.razorpay_order_id},
                status_code=status.HTTP_409_CONFLICT,
            )

        resource = payload.validated_data["resource"]
        quantity = payload.validated_data["quantity"]
        try:
            quote = pricing.quote_addon(subscription, resource, quantity)
        except pricing.PricingError as exc:
            return self.error(message=str(exc), errors=["ADDON_UNAVAILABLE"],
                              status_code=status.HTTP_409_CONFLICT)

        # Late in the cycle the prorated amount can fall below what a gateway will
        # accept. Refusing would be absurd for a purchase the tenant wants, so the
        # capacity is granted outright and the full price starts at the next renewal.
        if quote.total < payments.MINIMUM_CHARGE:
            from onboard.services import entitlements as entitlement_service

            entitlement_service.apply_addon(
                tenant_id=tenant.id, subscription=subscription,
                resource=resource, additional_units=quantity,
            )
            return self.success(
                data={"charged": False, "breakdown": quote.as_breakdown()},
                message=(
                    "Capacity added at no charge for the remainder of this cycle; the "
                    "full amount applies from your next renewal."
                ),
            )

        try:
            transaction = self._create_order(
                tenant=tenant, subscription=subscription, quote=quote,
                transaction_type=TxnType.ADDON,
                receipt=f"addon_{resource}_{quantity}",
            )
        except Exception:
            return self.error(message="Could not reach the payment gateway.",
                              errors=["GATEWAY_ERROR"],
                              status_code=status.HTTP_502_BAD_GATEWAY)

        return self._order_response(transaction, quote, "Add-on order created")
