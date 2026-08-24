from rest_framework.views import APIView
from rest_framework import status
from rest_framework.permissions import AllowAny
from django.conf import settings
from django.contrib.auth import get_user_model
import sys

from utils.views.base_model_viewset import ResponseMixin
from onboard.models import TempUser, BillingAddress, Transaction, RazorpayWebhookLog
from onboard.views.onboarding.base import RAZORPAY_KEY_ID, razorpay_client, create_tenant_and_mapping
from onboard.serializers.onboarding import RazorpayWebhookSerializer, GeneralResponseSerializer
from drf_spectacular.utils import extend_schema

User = get_user_model()


@extend_schema(
    tags=["Onboarding - Webhooks"],
    summary="Razorpay Webhook Handler",
    description="Receives and processes server-to-server transaction status update webhooks from Razorpay.",
    responses={
        200: GeneralResponseSerializer,
        400: GeneralResponseSerializer
    }
)
class RazorpayWebhookView(ResponseMixin, APIView):
    permission_classes = [AllowAny]
    serializer_class = RazorpayWebhookSerializer

    def post(self, request):
        payload = request.body
        signature = request.headers.get("X-Razorpay-Signature")

        # Parse event details
        event_id = request.data.get("id")
        event = request.data.get("event")

        # An explicit setting, not an inference from what a credential happens to
        # equal. `test in sys.argv` stays as a second guard for suites that do not
        # load the test settings module.
        is_testing = getattr(settings, "TESTING", False) or "test" in sys.argv
        if is_testing or settings.PAYMENTS_ALLOW_UNSIGNED:
            signature_valid = True
        else:
            try:
                razorpay_client.utility.verify_webhook_signature(
                    payload, signature, settings.RAZORPAY_WEBHOOK_SECRET
                )
                signature_valid = True
            except Exception:
                signature_valid = False

        # Create our webhook log entry
        log_entry = RazorpayWebhookLog.objects.create(
            event_id=event_id,
            event_type=event,
            signature=signature,
            is_signature_valid=signature_valid,
            payload=request.data
        )

        if not signature_valid:
            log_entry.error_message = "Cryptographic webhook signature check failed."
            log_entry.save()
            return self.error(
                message="unauthorized",
                errors=["SIGNATURE_CHECK_FAILED"],
                status_code=status.HTTP_400_BAD_REQUEST
            )

        # Process event details safely with Try/Except to log internal processing failures
        try:
            if event == "payment.captured":
                payment_entity = request.data["payload"]["payment"]["entity"]
                order_id = payment_entity["order_id"]
                payment_id = payment_entity["id"]

                try:
                    transaction = Transaction.objects.get(razorpay_order_id=order_id)
                    if transaction.status != Transaction.TransactionStatusChoices.SUCCESSFUL:
                        # Perform fail-safe promotion in background
                        transaction.razorpay_payment_id = payment_id
                        transaction.status = Transaction.TransactionStatusChoices.SUCCESSFUL
                        transaction.save()

                        temp_user = transaction.temp_user
                        if temp_user:
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
                                language=temp_user.language
                            )
                            user.save()

                            # Create Tenant and Tenant User Mapping cleanly
                            tenant = create_tenant_and_mapping(temp_user, user)

                            # Subscription, rate card and the cycle's credits, in one
                            # place. Idempotent against verify_payment having already
                            # completed the same onboarding from the browser.
                            from onboard.services import subscriptions as subscription_service
                            subscription_service.activate(
                                tenant, temp_user.selected_plan, transaction=transaction
                            )

                            BillingAddress.objects.filter(temp_user=temp_user).update(user=user, temp_user=None)
                            transaction.user = user
                            transaction.temp_user = None
                            # Mark the TempUser as onboarded and soft-delete instead of hard-deleting
                            temp_user.is_onboarded = True
                            temp_user.save()
                            temp_user.soft_delete()
                except Transaction.DoesNotExist:
                    pass

            elif event == "payment.failed":
                payment_entity = request.data["payload"]["payment"]["entity"]
                order_id = payment_entity["order_id"]
                try:
                    transaction = Transaction.objects.get(razorpay_order_id=order_id)
                    transaction.status = Transaction.TransactionStatusChoices.FAILED
                    transaction.raw_webhook_payload = request.data
                    transaction.save()
                except Transaction.DoesNotExist:
                    pass

        except Exception as e:
            # Capture any processing crash context inside the audit log entry
            log_entry.error_message = f"Processing failed: {str(e)}"
            log_entry.save()
            raise e

        return self.success(
            message="success",
            status_code=status.HTTP_200_OK
        )

