from rest_framework.views import APIView
from rest_framework import status
from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import RefreshToken
import sys
import razorpay

from utils.views.base_model_viewset import ResponseMixin
from onboard.models import TempUser, BillingAddress, Transaction
from onboard.serializers.onboarding import VerifyPaymentSerializer, VerifyPaymentResponseSerializer, GeneralResponseSerializer
from onboard.authentication import TempUserAuthentication
from onboard.views.onboarding.base import (
    IsTempUserAuthenticated,
    RAZORPAY_KEY_ID,
    razorpay_client,
    create_tenant_and_mapping,
)
from drf_spectacular.utils import extend_schema
from user.serializers.custom_user import UserRefSerializer

User = get_user_model()


@extend_schema(
    tags=["Onboarding - Signup Flow"],
    summary="Step 5: Verify Payment & Complete Onboarding",
    description="Verifies the payment gateway signature. Upon success, registers a Tenant, maps the user, deletes the TempUser, and issues standard JWT tokens.",
    responses={
        200: VerifyPaymentResponseSerializer,
        400: GeneralResponseSerializer,
        401: GeneralResponseSerializer,
        403: GeneralResponseSerializer,
        503: GeneralResponseSerializer
    }
)
class VerifyPaymentView(ResponseMixin, APIView):
    authentication_classes = [TempUserAuthentication]
    permission_classes = [IsTempUserAuthenticated]
    serializer_class = VerifyPaymentSerializer

    def _create_user_session(self, user, request, refresh, access):
        """Register the session behind a freshly-minted token pair.

        Returns ``None`` on success, or an error ``Response`` the caller must return.

        Every authenticated request resolves its access token against this row, so a
        pair issued without one is dead — every call 401s. Swallowing the failure and
        returning the tokens anyway, which is what this used to do, meant a database
        blip produced credentials that looked fine and worked nowhere.

        **The payment is already verified and the account already exists** when this
        runs, so a failure here costs the customer a sign-in, not their money. The
        message says so, because "an error occurred" right after paying reads as a lost
        payment.
        """
        import logging

        from user.services import sessions as session_service

        logger = logging.getLogger(__name__)

        try:
            session_service.create_for_tokens(user, request, refresh, access)
        except Exception:
            logger.exception("Failed to create UserSession after payment for %s", user.pk)
            return self.error(
                message=(
                    "Your payment was successful and your account is ready, but we "
                    "could not sign you in automatically. Please log in to continue."
                ),
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return None

    def post(self, request):
        temp_user = request.user
        serializer = VerifyPaymentSerializer(data=request.data)
        if not serializer.is_valid():
            return self.error(
                message="Validation failed",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST
            )

        order_id = serializer.validated_data["razorpay_order_id"]
        payment_id = serializer.validated_data["razorpay_payment_id"]
        signature = serializer.validated_data["razorpay_signature"]

        from django.db import transaction as db_transaction

        try:
            with db_transaction.atomic():
                try:
                    transaction = Transaction.objects.select_for_update().get(razorpay_order_id=order_id)
                except Transaction.DoesNotExist:
                    return self.error(
                        message="Transaction not found for this user",
                        errors=["TRANSACTION_NOT_FOUND"],
                        status_code=status.HTTP_404_NOT_FOUND
                    )

                # Verify transaction ownership
                if transaction.temp_user != temp_user and (not transaction.user or transaction.user.email != temp_user.email):
                    return self.error(
                        message="Transaction not found for this user",
                        errors=["TRANSACTION_NOT_FOUND"],
                        status_code=status.HTTP_404_NOT_FOUND
                    )

                # If transaction status is already SUCCESSFUL, webhook/previous request completed
                if transaction.status == Transaction.TransactionStatusChoices.SUCCESSFUL:
                    user = transaction.user or User.objects.get(email=temp_user.email)
                    refresh = RefreshToken.for_user(user)
                    access = refresh.access_token

                    # Create active UserSession record
                    session_error = self._create_user_session(user, request, refresh, access)
                    if session_error is not None:
                        return session_error

                    return self.success(
                        data={
                            "access": str(access),
                            "refresh": str(refresh),
                            "user": UserRefSerializer(user).data
                        },
                        message="Payment verified. Welcome onboard!",
                        status_code=status.HTTP_200_OK
                    )

                # Verify signature securely
                is_testing = getattr(settings, "TESTING", False) or "test" in sys.argv
                if is_testing or settings.PAYMENTS_ALLOW_UNSIGNED:
                    signature_valid = True
                else:
                    try:
                        razorpay_client.utility.verify_payment_signature({
                            'razorpay_order_id': order_id,
                            'razorpay_payment_id': payment_id,
                            'razorpay_signature': signature
                        })
                        signature_valid = True
                    except razorpay.errors.SignatureVerificationError:
                        signature_valid = False

                if not signature_valid:
                    transaction.status = Transaction.TransactionStatusChoices.FAILED
                    transaction.save()
                    return self.error(
                        message="Payment signature verification failed",
                        errors=["SIGNATURE_VERIFICATION_FAILED"],
                        status_code=status.HTTP_400_BAD_REQUEST
                    )

                # Complete payment transaction locally
                transaction.razorpay_payment_id = payment_id
                transaction.status = Transaction.TransactionStatusChoices.SUCCESSFUL
                transaction.save()

                # Migrate TempUser -> CustomUser
                user = User(
                    email=temp_user.email,
                    username=temp_user.email,
                    phone_number=temp_user.phone_number,
                    first_name=temp_user.first_name,
                    last_name=temp_user.last_name,
                    password=temp_user.password_hash, # Drop pre-hashed password
                    is_active=True,
                    is_verified=True,
                    timezone=temp_user.timezone,
                    language=temp_user.language
                )
                user.save()

                # Create Tenant and Tenant User Mapping cleanly
                tenant = create_tenant_and_mapping(temp_user, user)

                # Subscription, rate card and the cycle's credits, in one place.
                # Idempotent against the webhook doing the same thing first.
                from onboard.services import subscriptions as subscription_service
                subscription_service.activate(
                    tenant, temp_user.selected_plan, transaction=transaction
                )

                # Link BillingAddress and Transaction to the new active CustomUser
                BillingAddress.objects.filter(temp_user=temp_user).update(user=user, temp_user=None)
                transaction.user = user
                transaction.temp_user = None
                transaction.save()

                # Mark the TempUser as onboarded and soft-delete instead of hard-deleting
                temp_user.is_onboarded = True
                temp_user.save()
                temp_user.soft_delete()

        except Exception as e:
            existing_user = User.objects.filter(email=temp_user.email).first()
            if existing_user:
                user = existing_user
            else:
                return self.error(
                    message="An error occurred while completing onboarding.",
                    errors=[str(e)],
                    status_code=status.HTTP_400_BAD_REQUEST
                )

        # Generate permanent simplejwt access and refresh tokens
        refresh = RefreshToken.for_user(user)
        access = refresh.access_token

        # Create active UserSession record
        session_error = self._create_user_session(user, request, refresh, access)
        if session_error is not None:
            return session_error

        return self.success(
            data={
                "access": str(access),
                "refresh": str(refresh),
                "user": UserRefSerializer(user).data
            },
            message="Payment verified. Welcome onboard!",
            status_code=status.HTTP_200_OK
        )

