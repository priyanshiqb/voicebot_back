import logging
import sys

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction as db_transaction
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from onboard.models import BillingAddress, RazorpayWebhookLog, Transaction
from onboard.serializers.onboarding import (
    GeneralResponseSerializer,
    RazorpayWebhookSerializer,
)
from onboard.views.onboarding.base import create_tenant_and_mapping, razorpay_client
from utils.views.base_model_viewset import ResponseMixin

logger = logging.getLogger(__name__)
User = get_user_model()

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

        transaction.razorpay_payment_id = entity["id"]
        transaction.status = TxnStatus.SUCCESSFUL
        transaction.raw_webhook_payload = data
        transaction.save()

        handler = {
            TxnType.ONBOARDING: self._apply_onboarding,
            TxnType.TOP_UP: self._apply_top_up,
            TxnType.ADDON: self._apply_addon,
            TxnType.RENEWAL: self._apply_renewal,
            TxnType.UPGRADE: self._apply_upgrade,
        }.get(transaction.transaction_type, self._apply_unimplemented)

        handler(transaction)

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

    # ------------------------------------------------------------- handlers

    def _apply_onboarding(self, transaction):
        """Promote the pending registrant into a real account.

        The browser-confirmed path (`verify_payment`) does the same work, and either
        can win the race. `activate()` is idempotent, and this returns early when the
        TempUser has already been consumed.
        """
        from onboard.services import subscriptions as subscription_service

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

    def _apply_top_up(self, transaction):
        """Deposit purchased credits.

        Into the **purchased** bucket, which rollover rules never touch — these were
        bought with cash, and expiring them at a cycle boundary would be taking money
        for nothing. The grant is keyed on the order id, so a redelivery credits once.
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

        # Only on a real grant — a redelivered webhook returns None, and telling a
        # tenant twice that their credits arrived is worse than not telling them.
        if entry is not None:
            from onboard.services import billing_notifications

            try:
                billing_notifications.notify_credits_added(
                    transaction.tenant_id, credits=credits,
                    reason="Your credit top-up has been applied",
                )
            except Exception:
                logger.warning("top-up notification failed", exc_info=True)

    def _apply_addon(self, transaction):
        """Grant the extra capacity that was paid for.

        Quantities come from the breakdown frozen when the order was created, not
        from a fresh calculation: the tenant paid for what they were quoted, and the
        catalogue may have moved since.
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

    def _apply_renewal(self, transaction):
        """Extend the cycle and fund it."""
        from onboard.services import subscriptions as subscription_service

        if transaction.subscription is None:
            logger.error(
                "renewal order %s has no subscription attached; cycle NOT extended",
                transaction.razorpay_order_id,
            )
            return

        subscription_service.renew(transaction.subscription, transaction=transaction)

    def _apply_upgrade(self, transaction):
        """Move the tenant to the plan they just paid the difference for.

        Any scheduled downgrade is dropped first: one set weeks ago would otherwise
        fire later and silently undo the upgrade they have just bought.
        """
        from onboard.services import subscriptions as subscription_service

        subscription = transaction.subscription
        if subscription is None or transaction.target_plan is None:
            logger.error(
                "upgrade order %s is missing its subscription or target plan; plan "
                "NOT changed",
                transaction.razorpay_order_id,
            )
            return

        subscription_service.cancel_scheduled_change(subscription)
        subscription_service.apply_plan_change(
            subscription,
            transaction.target_plan,
            transaction=transaction,
            reason="upgrade",
        )

    def _apply_unimplemented(self, transaction):
        """A payment type whose handler has not been built yet.

        Loud rather than silent, and — importantly — **not** routed to onboarding.
        Before this dispatch existed, every captured payment ran the signup path, so a
        top-up would have tried to promote a TempUser that does not exist. The
        transaction is still marked successful, so no money is lost track of; what is
        missing is the credits or plan change it should have produced.
        """
        logger.error(
            "no handler for %s payment on order %s — payment recorded, effect NOT "
            "applied",
            transaction.transaction_type,
            transaction.razorpay_order_id,
        )
