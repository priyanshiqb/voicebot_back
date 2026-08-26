"""A tenant's own payment history, and what they can still do about it.

Two problems this solves. The first is simply that there was no way to see what you
had been charged for. The second is sharper: only one unpaid order may be open at a
time, and when a purchase is blocked the message says *"complete or cancel it"* — but
there was no cancel, and no way to resume the order either. A tenant who closed the
Checkout tab could only wait out the 30-minute TTL.

So `cancel` and `retry` are not conveniences bolted onto a list view; they are the
two exits from a state the tenant could previously only sit in.
"""

import logging

import django_filters
from django.db import transaction as db_transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.decorators import action

from onboard.models import Transaction
from onboard.serializers.transaction import (
    CheckoutPayloadSerializer,
    TransactionDetailSerializer,
    TransactionListSerializer,
)
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet

logger = logging.getLogger(__name__)

TxnType = Transaction.TransactionTypeChoices
TxnStatus = Transaction.TransactionStatusChoices

#: How much longer a resumed order stays payable. Matches ORDER_TTL — retrying is
#: starting the clock again, not extending it indefinitely.
RETRY_TTL_MINUTES = 30


class TransactionFilter(django_filters.FilterSet):
    """"What did I pay in March", and "what is still unpaid" — the two questions this
    endpoint exists to answer."""

    created_after = django_filters.DateTimeFilter(
        field_name="created_at", lookup_expr="gte"
    )
    created_before = django_filters.DateTimeFilter(
        field_name="created_at", lookup_expr="lte"
    )

    class Meta:
        model = Transaction
        fields = ["status", "transaction_type", "created_after", "created_before"]


@extend_schema_view(
    list=extend_schema(
        tags=["Billing - Purchases"],
        summary="Payment history",
        description=(
            "Every payment this tenant has made or attempted, newest first — signup, "
            "renewals, plan changes, credit top-ups and capacity add-ons.\n\n"
            "Each row carries `can_cancel` and `can_retry`, so the frontend does not "
            "have to reimplement the rule for which unpaid orders are still live.\n\n"
            "Filter by `status`, `transaction_type`, `created_after` / "
            "`created_before`."
        ),
    ),
    retrieve=extend_schema(
        tags=["Billing - Purchases"],
        summary="One payment",
        description=(
            "Looked up by `razorpay_order_id`, not by database id — that is the "
            "identifier every other part of the payment flow speaks, including the "
            "`409 PENDING_ORDER_EXISTS` that sends a tenant here in the first place."
        ),
    ),
)
class TransactionViewSet(TenantBaseViewSet):
    """Read-only, plus the two actions that resolve an unpaid order.

    Tenant scoping comes from `TenantBaseViewSet`, so a tenant only ever sees rows
    where `tenant` is their own. Signup payments appear here too — that first charge
    is part of their history, and hiding it would make the list look like it starts
    mid-story.
    """

    http_method_names = ["get", "post", "head", "options"]

    queryset = Transaction.objects.all()
    serializer_class = TransactionDetailSerializer
    list_serializer_class = TransactionListSerializer
    dropdown_serializer_class = None

    lookup_field = "razorpay_order_id"
    lookup_value_regex = "[^/]+"

    filterset_class = TransactionFilter
    search_fields = ["razorpay_order_id", "razorpay_payment_id"]
    ordering_fields = ["created_at", "amount", "status"]
    ordering = ["-created_at"]

    select_related_fields = ["plan", "target_plan"]

    @classmethod
    def get_extra_actions(cls):
        """Drop the base viewset's `bulk_delete` and `dropdown` from the router.

        `http_method_names` cannot do this job here. A read-only viewset blocks POST
        outright and gets it for free -- but `cancel` and `retry` need POST, so
        allowing the verb re-exposes `bulk_delete`, and a tenant being able to erase
        their own payment records is not a feature. `dropdown` is merely meaningless
        for transactions; `bulk_delete` is the one that matters.

        Filtered at route registration rather than overridden to 405, so the actions
        never appear in the OpenAPI document either.
        """
        return [
            a for a in super().get_extra_actions()
            if a.__name__ not in {"bulk_delete", "dropdown"}
        ]

    # ------------------------------------------------------------------ #

    def _open_order_or_error(self, verb):
        """The locked transaction if it can still be acted on, else an error Response.

        Returns ``(transaction, None)`` or ``(None, response)``. Locked because both
        callers mutate it, and a webhook can be settling the same row at this instant.
        """
        obj = self.get_object()
        locked = (
            Transaction.objects.select_for_update().filter(pk=obj.pk).first()
        )

        if locked.transaction_type == TxnType.ONBOARDING:
            return None, self.error(
                message=(
                    "Signup payments are managed through the onboarding flow, not "
                    "here."
                ),
                errors=["WRONG_ENDPOINT"],
                status_code=status.HTTP_409_CONFLICT,
            )

        if locked.status != TxnStatus.PENDING:
            return None, self.error(
                message=(
                    f"This order is already {locked.get_status_display().lower()} "
                    f"and cannot be {verb}."
                ),
                errors=["ORDER_NOT_PENDING"],
                data={"status": locked.status},
                status_code=status.HTTP_409_CONFLICT,
            )

        return locked, None

    @extend_schema(
        tags=["Billing - Purchases"],
        summary="Cancel an unpaid order",
        description=(
            "Abandons an order that was never paid, releasing the one-open-order "
            "lock so the tenant can start a different purchase immediately.\n\n"
            "**The row is kept, not deleted** — it stays in the history as "
            "`canceled`, because a payment record that disappears is worse than one "
            "that says nothing happened."
        ),
        request=None,
        responses={200: TransactionDetailSerializer},
    )
    @action(detail=True, methods=["post"])
    def cancel(self, request, *args, **kwargs):
        with db_transaction.atomic():
            locked, error = self._open_order_or_error("cancelled")
            if error:
                return error

            locked.status = TxnStatus.CANCELED
            locked.save(update_fields=["status", "updated_at"])

        locked.refresh_from_db()
        return self.success(
            data=TransactionDetailSerializer(locked).data,
            message="Order cancelled",
        )

    @extend_schema(
        tags=["Billing - Purchases"],
        summary="Resume paying an unpaid order",
        description=(
            "Returns the Checkout payload for an order that was created but never "
            "paid, so the tenant can reopen Razorpay and finish.\n\n"
            "**The same Razorpay order is reused**, not a new one. That keeps the "
            "order id stable, which is what every idempotency guard downstream is "
            "keyed on — a fresh order for the same intent would risk crediting a "
            "tenant twice if both were eventually paid."
        ),
        request=None,
        responses={200: CheckoutPayloadSerializer},
    )
    @action(detail=True, methods=["post"])
    def retry(self, request, *args, **kwargs):
        from django.conf import settings

        with db_transaction.atomic():
            locked, error = self._open_order_or_error("resumed")
            if error:
                return error

            # The clock restarts. An order the tenant is actively trying to pay
            # should not expire out from under them mid-Checkout because the
            # original 30 minutes happened to be nearly up.
            locked.expires_at = timezone.now() + timezone.timedelta(
                minutes=RETRY_TTL_MINUTES
            )
            locked.save(update_fields=["expires_at", "updated_at"])

        return self.success(
            data={
                "razorpay_order_id": locked.razorpay_order_id,
                "amount": int(locked.amount * 100),
                "amount_display": str(locked.amount),
                "currency": "INR",
                "razorpay_key_id": settings.RAZORPAY_KEY_ID,
                "expires_at": locked.expires_at,
                "breakdown": locked.amount_breakdown,
            },
            message="Order ready to pay",
        )
