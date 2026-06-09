from rest_framework.views import APIView
from rest_framework import status
from rest_framework.permissions import AllowAny

from utils.views.base_model_viewset import ResponseMixin
from onboard.models import TempUser
from onboard.authentication import generate_temp_token
from onboard.serializers.onboarding import VerifyOTPSerializer, VerifyOTPResponseSerializer, GeneralResponseSerializer
from drf_spectacular.utils import extend_schema


@extend_schema(
    tags=["Onboarding - Signup Flow"],
    summary="Step 2: Email OTP Verification",
    description="Verifies the emailed 6-digit OTP. Upon success, returns a temporary scoped token and the initial pricing breakdown.",
    responses={
        200: VerifyOTPResponseSerializer,
        400: GeneralResponseSerializer
    }
)
class VerifyOTPView(ResponseMixin, APIView):
    permission_classes = [AllowAny]
    serializer_class = VerifyOTPSerializer

    def post(self, request):
        temp_id = request.data.get("temp_registration_id")
        otp_code = request.data.get("otp_code")

        try:
            temp_user = TempUser.objects.get(id=temp_id)
        except (TempUser.DoesNotExist, ValueError):
            return self.error(
                message="Invalid registration ID",
                errors=["REGISTRATION_NOT_FOUND"],
                status_code=status.HTTP_400_BAD_REQUEST
            )

        if temp_user.otp_code != otp_code:
            return self.error(
                message="Invalid OTP code",
                errors=["INVALID_OTP"],
                status_code=status.HTTP_400_BAD_REQUEST
            )

        if temp_user.is_otp_expired():
            return self.error(
                message="Verification code has expired",
                errors=["OTP_EXPIRED"],
                status_code=status.HTTP_400_BAD_REQUEST
            )

        temp_user.is_email_verified = True
        temp_user.save()

        # Generate a temporary JWT-scoped authentication token
        temp_token = generate_temp_token(temp_user)

        # Get initial pricing breakdown of their selected plan
        plan = temp_user.selected_plan
        billing_breakdown = {
            "base_price": str(plan.base_price),
            "discount_amount": str(plan.discount_amount),
            "net_base_price": str(plan.net_base_price),
            "tax_percentage": str(plan.tax_percentage),
            "tax_amount": str(plan.tax_amount),
            "total_price": str(plan.total_price),
            "currency": plan.currency
        }

        return self.success(
            data={
                "temporary_token": temp_token,
                "email": temp_user.email,
                "plan": {
                    "id": str(plan.id),
                    "name": plan.name,
                    "code": plan.code
                },
                "billing_breakdown": billing_breakdown
            },
            message="Email verified successfully",
            status_code=status.HTTP_200_OK
        )

