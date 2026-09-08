from rest_framework.views import APIView
from rest_framework import status

from utils.views.base_model_viewset import ResponseMixin
from onboard.models import BillingAddress, Transaction
from onboard.authentication import TempUserAuthentication, generate_temp_token
from onboard.views.onboarding.base import (
    IsTempUserAuthenticated,
    billing_breakdown as build_breakdown,
    empty_billing_breakdown as empty_breakdown,
)
from onboard.serializers.onboarding import (
    BillingAddressSerializer,
    ResumeSessionResponseSerializer,
    GeneralResponseSerializer,
)
from drf_spectacular.utils import extend_schema


@extend_schema(
    tags=["Onboarding - Signup Flow"],
    summary="Step 2 Helper: Resume Onboarding Session with Token",
    description="Allows a client to resume their onboarding process from their last completed state. Pass the temporary token via the Authorization header as 'Bearer <token>'.",
    request=None,
    responses={
        200: ResumeSessionResponseSerializer,
        401: GeneralResponseSerializer,
    }
)
class ResumeSessionWithTokenView(ResponseMixin, APIView):
    authentication_classes = [TempUserAuthentication]
    permission_classes = [IsTempUserAuthenticated]

    def post(self, request):
        temp_user = request.user

        # Get their selected plan details
        plan = temp_user.selected_plan
        billing_breakdown = (
            build_breakdown(plan, temp_user.selected_minutes)
            if plan else empty_breakdown()
        )

        # Check if they have already saved a billing address
        billing_address = BillingAddress.objects.filter(temp_user=temp_user).first()
        billing_address_data = None
        if billing_address:
            billing_address_data = BillingAddressSerializer(billing_address).data

        # Check if they have an active pending order
        pending_transaction = Transaction.objects.filter(
            temp_user=temp_user, status=Transaction.TransactionStatusChoices.PENDING
        ).first()

        # Determine the next flow step dynamically based on their saved data
        if not temp_user.is_email_verified:
            step = "OTP_VERIFICATION_REQUIRED"
        elif not billing_address:
            step = "BILLING_ADDRESS_REQUIRED"
        elif not pending_transaction:
            step = "PROCEED_TO_PAYMENT"
        else:
            step = "PAYMENT_VERIFICATION_REQUIRED"

        # Reissue a fresh temporary token to keep session active
        temp_token = generate_temp_token(temp_user)

        return self.success(
            data={
                "temporary_token": temp_token,
                "temp_registration_id": str(temp_user.id),
                "email": temp_user.email,
                "phone_number": temp_user.phone_number,
                "step": step,
                "plan": {
                    "id": str(plan.id) if plan else None,
                    "name": plan.name if plan else None,
                    "code": plan.code if plan else None
                } if plan else None,
                "selected_minutes": temp_user.selected_minutes,
                "billing_breakdown": billing_breakdown,
                "billing_address": billing_address_data,
                "razorpay_order_id": pending_transaction.razorpay_order_id if pending_transaction else None
            },
            message="Session resumed successfully.",
            status_code=status.HTTP_200_OK
        )
