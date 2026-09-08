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

        # What this plan costs for the quantity the registrant chose. `quote_plan`
        # for a minutes-based plan; the catalogue price for a legacy one.
        from onboard.services import pricing

        if plan.is_minutes_based:
            try:
                quote = pricing.quote_plan(plan, temp_user.selected_minutes)
            except pricing.PricingError as exc:
                return self.error(
                    message=str(exc),
                    errors=["MINUTES_NOT_SELLABLE"],
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            amount, breakdown = quote.total, quote.as_breakdown()
        else:
            amount, breakdown = plan.total_price, None

        amount_in_paisa = int(amount * 100)

        # Handle 0 amount (free trial/tier) checkouts by skipping Razorpay completely
        if amount_in_paisa == 0:
            from rest_framework_simplejwt.tokens import RefreshToken

            from onboard.services import payment_application

            transaction = Transaction.objects.create(
                temp_user=temp_user,
                plan=plan,
                razorpay_order_id=f"free_{uuid.uuid4().hex[:14]}",
                amount=amount,
                amount_breakdown=breakdown,
                status=Transaction.TransactionStatusChoices.SUCCESSFUL
            )

            # **The shared promotion, not a local copy of it.** This branch used to
            # inline its own TempUser -> CustomUser migration, and it had already
            # drifted from `_apply_onboarding`: it dropped the registrant's timezone
            # and language, and never set `transaction.tenant`, so a free signup's
            # first payment was missing from their own billing history. Carrying a
            # third copy through the minutes change would have guaranteed a fourth
            # difference.
            payment_application.apply(transaction)

            transaction.refresh_from_db()
            user = transaction.user

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

        # Save Transaction record locally. The breakdown freezes the two per-minute
        # figures, so activation grants what was quoted even if the rate card moves
        # between now and the payment clearing.
        Transaction.objects.create(
            temp_user=temp_user,
            plan=plan,
            razorpay_order_id=razorpay_order_id,
            amount=amount,
            amount_breakdown=breakdown,
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

