from rest_framework.views import APIView
from rest_framework import status
from django.conf import settings
import uuid
import sys

from utils.views.base_model_viewset import ResponseMixin
from onboard.models import BillingAddress, Transaction
from onboard.serializers.onboarding import (
    BillingAddressSerializer,
    CreateOrderJustInTimeSerializer,
    CreateOrderJustInTimeResponseSerializer,
    GeneralResponseSerializer,
)
from onboard.authentication import TempUserAuthentication
from onboard.views.onboarding.base import (
    IsTempUserAuthenticated,
    RAZORPAY_KEY_ID,
    razorpay_client,
)
from drf_spectacular.utils import extend_schema
from user.serializers.custom_user import UserRefSerializer


@extend_schema(
    tags=["Onboarding - Signup Flow"],
    summary="Step 4: Create Razorpay Order Just-In-Time",
    description="Captures the billing address, validates the GSTIN number (if provided), and generates a new Razorpay payment gateway order.",
    responses={
        200: CreateOrderJustInTimeResponseSerializer,
        400: GeneralResponseSerializer,
        401: GeneralResponseSerializer,
        403: GeneralResponseSerializer
    }
)
class CreateOrderJustInTimeView(ResponseMixin, APIView):
    authentication_classes = [TempUserAuthentication]
    permission_classes = [IsTempUserAuthenticated]
    serializer_class = CreateOrderJustInTimeSerializer

    def post(self, request):
        temp_user = request.user
        plan = temp_user.selected_plan

        if not plan:
            return self.error(
                message="No plan selected",
                errors=["PLAN_SELECTION_REQUIRED"],
                status_code=status.HTTP_400_BAD_REQUEST
            )

        # Validate and save Billing Address and phone_number
        # `context` is required, not optional: validate_phone_number excludes the
        # CURRENT registration from the "already pending" check, and it identifies that
        # registration through request.user. Without the context the exclusion silently
        # does nothing, so the second call to this endpoint rejects the caller's own
        # phone number — which is exactly what happens when someone edits their billing
        # address or retries after a failed payment.
        serializer = CreateOrderJustInTimeSerializer(
            data=request.data, context={"request": request}
        )
        if not serializer.is_valid():
            return self.error(
                message="Validation failed",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST
            )

        phone_number = serializer.validated_data["phone_number"]
        billing_address_data = serializer.validated_data["billing_address"]

        # Update phone number on TempUser
        temp_user.phone_number = phone_number
        temp_user.save()

        # Keep only one billing address for the TempUser
        BillingAddress.all_objects.filter(temp_user=temp_user).delete()
        address_serializer = BillingAddressSerializer(data=billing_address_data)
        address_serializer.is_valid(raise_exception=True)
        address_serializer.save(temp_user=temp_user)

        # Calculate pricing in Paisa for Razorpay
        amount_in_paisa = int(plan.total_price * 100)

        # Handle 0 amount (free trial/tier) checkouts by skipping Razorpay completely
        if amount_in_paisa == 0:
            # Save local successful transaction record
            transaction = Transaction.objects.create(
                temp_user=temp_user,
                plan=plan,
                razorpay_order_id=f"free_{uuid.uuid4().hex[:14]}",
                amount=plan.total_price,
                status=Transaction.TransactionStatusChoices.SUCCESSFUL
            )

            # Migrate TempUser -> CustomUser
            from django.contrib.auth import get_user_model
            from rest_framework_simplejwt.tokens import RefreshToken
            from onboard.views.onboarding.base import create_tenant_and_mapping
            
            User = get_user_model()
            user = User(
                email=temp_user.email,
                username=temp_user.email,
                phone_number=temp_user.phone_number,
                first_name=temp_user.first_name,
                last_name=temp_user.last_name,
                password=temp_user.password_hash,
                is_active=True,
                is_verified=True
            )
            user.save()

            # Create Tenant and Tenant User Mapping cleanly
            tenant = create_tenant_and_mapping(temp_user, user)

            # Create Subscription record for the new tenant with a plan snapshot
            from onboard.models import Subscription
            Subscription.objects.create(
                tenant=tenant,
                plan=plan,
                status=Subscription.SubscriptionStatusChoices.ACTIVE
            )

            # Link BillingAddress and Transaction to the new active CustomUser
            BillingAddress.objects.filter(temp_user=temp_user).update(user=user, temp_user=None)
            transaction.user = user
            transaction.temp_user = None
            transaction.save()

            # Mark the TempUser as onboarded and soft-delete
            temp_user.is_onboarded = True
            temp_user.save()
            temp_user.soft_delete()

            # Generate permanent simplejwt access and refresh tokens
            refresh = RefreshToken.for_user(user)
            access = refresh.access_token

            return self.success(
                data={
                    "is_free": True,
                    "access": str(access),
                    "refresh": str(refresh),
                    "user": UserRefSerializer(user).data
                },
                message="Free onboarding completed successfully!",
                status_code=status.HTTP_200_OK
            )

        # Handle testing / offline fallback gracefully
        # Stubbing the gateway is the same decision as accepting unsigned callbacks:
        # both mean "this environment is not talking to real Razorpay".
        is_testing = getattr(settings, "TESTING", False) or "test" in sys.argv
        if is_testing or settings.PAYMENTS_ALLOW_UNSIGNED:
            razorpay_order_id = f"order_{uuid.uuid4().hex[:14]}"
        else:
            try:
                razorpay_order = razorpay_client.order.create({
                    "amount": amount_in_paisa,
                    "currency": "INR",
                    "receipt": f"receipt_{plan.code}",
                    "payment_capture": 1
                })
                razorpay_order_id = razorpay_order["id"]
            except Exception as e:
                return self.error(
                    message=f"Razorpay connection error: {str(e)}",
                    errors=["RAZORPAY_ERROR"],
                    status_code=status.HTTP_502_BAD_GATEWAY
                )

        # Save Transaction record locally
        Transaction.objects.create(
            temp_user=temp_user,
            plan=plan,
            razorpay_order_id=razorpay_order_id,
            amount=plan.total_price,
            status=Transaction.TransactionStatusChoices.PENDING
        )

        return self.success(
            data={
                "razorpay_order_id": razorpay_order_id,
                "amount": amount_in_paisa,
                "currency": "INR",
                "razorpay_key_id": RAZORPAY_KEY_ID
            },
            message="Razorpay order initiated",
            status_code=status.HTTP_200_OK
        )

