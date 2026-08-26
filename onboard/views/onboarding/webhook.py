import logging
import sys

from django.conf import settings
from django.db import transaction as db_transaction
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from onboard.models import RazorpayWebhookLog, Transaction
from onboard.serializers.onboarding import (
    GeneralResponseSerializer,
    RazorpayWebhookSerializer,
)
from onboard.services import payment_application
from onboard.views.onboarding.base import razorpay_client
from utils.views.base_model_viewset import ResponseMixin

logger = logging.getLogger(__name__)

TxnType = Transaction.TransactionTypeChoices
TxnStatus = Transaction.TransactionStatusChoices


@extend_schema(
    tags=["Onboarding - Webhooks"],
    summary="Razorpay Webhook Handler",
    description=(
        "Receives server-to-server payment events from Razorpay and applies them to "
        "the matching transaction.\n\n"
        "Returns 200 for anything it has accepted responsibility for — including "
        "events it has already seen and events whose processing failed. Only a bad "
        "signature is a 4xx. Razorpay retries non-2xx responses, and retrying a "
        "payload that will fail identically achieves nothing but a duplicate charge "
        "risk."
    ),
    responses={200: GeneralResponseSerializer, 400: GeneralResponseSerializer},
)
class RazorpayWebhookView(ResponseMixin, APIView):
    """
    **Idempotent at two layers.**

    Razorpay retries aggressively, and a redelivered `payment.captured` used to run
    the whole onboarding promotion a second time. The outer guard is
    `RazorpayWebhookLog.event_id`, which is unique: a duplicate event is recognised
    at `get_or_create` and answered 200 without touching anything. The inner guard is
    the credit ledger's own `(entry_type, reference_id)` constraint, which catches a
    genuine redelivery carrying a fresh event id.

    **Dispatches on `Transaction.transaction_type`.** Previously every captured
    payment was assumed to be a signup, so a top-up or renewal would have tried to
    promote a TempUser that does not exist.
    """

    permission_classes = [AllowAny]
    serializer_class = RazorpayWebhookSerializer

    # ---------------------------------------------------------------- entry

    def post(self, request):
        # Read the raw body FIRST. DRF consumes the request stream when it parses
        # `request.data`, and `request.body` raises RawPostDataException afterwards.
        # The signature is computed over the exact bytes Razorpay sent, so it has to
        # come from here rather than from a re-serialised dict.
        raw_body = request.body

        signature = request.headers.get("X-Razorpay-Signature")
        event_id = request.data.get("id")
        event = request.data.get("event")

        signature_valid = self._verify(raw_body, signature)

        # get_or_create, not create: event_id is unique, and `created=False` is how a
        # redelivery is recognised.
        log_entry, created = RazorpayWebhookLog.objects.get_or_create(
            event_id=event_id,
            defaults={
                "event_type": event,
                "signature": signature,
                "is_signature_valid": signature_valid,
                "payload": request.data,
            },
        )

        if not created and event_id:
            logger.info("razorpay event %s already processed; ignoring redelivery", event_id)
            return self.success(
                message="already processed", status_code=status.HTTP_200_OK
            )

        if not signature_valid:
            log_entry.error_message = "Cryptographic webhook signature check failed."
            log_entry.save(update_fields=["error_message", "updated_at"])
            return self.error(
                message="unauthorized",
                errors=["SIGNATURE_CHECK_FAILED"],
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        try:
            with db_transaction.atomic():
                self._process(event, request.data)
        except Exception as exc:  # noqa: BLE001
            # Deliberately not re-raised. A 500 makes Razorpay redeliver a payload
            # that will fail in exactly the same way, forever — and every redelivery
            # is another chance to half-apply something. The error is recorded here
            # and the atomic block above has already rolled back whatever it started,
            # so the event can be replayed by hand once the cause is fixed.
            logger.exception("razorpay webhook %s failed processing", event_id)
            log_entry.error_message = f"Processing failed: {exc}"
            log_entry.save(update_fields=["error_message", "updated_at"])
            return self.success(
                message="accepted; processing failed and was logged",
                status_code=status.HTTP_200_OK,
            )

        return self.success(message="success", status_code=status.HTTP_200_OK)

    # ------------------------------------------------------------ signature

    def _verify(self, payload, signature):
        is_testing = getattr(settings, "TESTING", False) or "test" in sys.argv
        if is_testing or settings.PAYMENTS_ALLOW_UNSIGNED:
            return True
        try:
            razorpay_client.utility.verify_webhook_signature(
                payload, signature, settings.RAZORPAY_WEBHOOK_SECRET
            )
            return True
        except Exception:
            return False

    # -------------------------------------------------------------- routing

    def _process(self, event, data):
        if event == "payment.captured":
            return self._on_captured(data)
        if event == "payment.failed":
            return self._on_failed(data)
        logger.info("razorpay event %s ignored (no handler)", event)

    def _transaction_for(self, data):
        """The transaction this event refers to, locked, or ``None``.

        An unknown order is not an error: Razorpay sends events for orders this
        system never created, and test-mode traffic in particular.
        """
        entity = data["payload"]["payment"]["entity"]
        order_id = entity["order_id"]
        transaction = (
            Transaction.objects.select_for_update()
            .filter(razorpay_order_id=order_id)
            .first()
        )
        if transaction is None:
            logger.info("razorpay event references unknown order %s", order_id)
        return transaction, entity

    def _on_captured(self, data):
        transaction, entity = self._transaction_for(data)
        if transaction is None:
            return

        if transaction.status == TxnStatus.SUCCESSFUL:
            logger.info(
                "order %s already marked successful; nothing to apply",
                transaction.razorpay_order_id,
            )
            return

        # The amount is free to check here and nowhere else: this signature is an
        # HMAC over the whole raw body, so `entity["amount"]` is authenticated. The
        # browser path's signature covers only order_id|payment_id, which is why it
        # has to ask Razorpay instead.
        verdict = payment_application.check_amount(transaction, entity.get("amount"))

        transaction.razorpay_payment_id = entity["id"]
        transaction.raw_webhook_payload = data

        if verdict == "underpaid":
            # Recorded, not applied, and not FAILED — money did change hands. A
            # human decides between refunding it and collecting the difference.
            transaction.status = TxnStatus.UNDERPAID
            transaction.save()
            return

        transaction.status = TxnStatus.SUCCESSFUL
        transaction.save()

        # Shared with `/billing/verify-payment/`, which the browser calls when it
        # returns from Checkout. Either path can be the one that arrives, so the
        # effect has to live in one place or the two drift apart.
        payment_application.apply(transaction)

    def _on_failed(self, data):
        transaction, _ = self._transaction_for(data)
        if transaction is None:
            return
        if transaction.status == TxnStatus.SUCCESSFUL:
            # A failure event arriving after a capture is out-of-order delivery, not
            # a reversal. Marking it failed would contradict money that was taken.
            logger.warning(
                "ignoring payment.failed for already-successful order %s",
                transaction.razorpay_order_id,
            )
            return
        transaction.status = TxnStatus.FAILED
        transaction.raw_webhook_payload = data
        transaction.save()
