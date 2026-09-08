from rest_framework.views import APIView
from rest_framework import status
from rest_framework.permissions import AllowAny

from utils.views.base_model_viewset import ResponseMixin
from onboard.models import TempUser
from onboard.authentication import generate_temp_token
from onboard.views.onboarding.base import billing_breakdown as build_breakdown
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

        # Check brute force attempts
        from django.core.cache import cache
        cache_key = f"onboard_otp_signup_attempts:{temp_user.id}"
        attempts = cache.get(cache_key, 0)

        if attempts >= 5:
            if temp_user.otp_code is not None:
                temp_user.otp_code = None
                temp_user.otp_created_at = None
                temp_user.save()
            return self.error(
                message="Too many failed attempts. Verification code has been invalidated.",
                errors=["TOO_MANY_ATTEMPTS"],
                status_code=status.HTTP_400_BAD_REQUEST
            )

        if temp_user.otp_code != otp_code:
            attempts += 1
            cache.set(cache_key, attempts, 600)  # 10 minutes lock duration
            if attempts >= 5:
                temp_user.otp_code = None
                temp_user.otp_created_at = None
                temp_user.save()
                return self.error(
                    message="Too many failed attempts. Verification code has been invalidated.",
                    errors=["TOO_MANY_ATTEMPTS"],
                    status_code=status.HTTP_400_BAD_REQUEST
                )
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

        # Invalidate OTP attempts cache upon success
        cache.delete(cache_key)

        temp_user.is_email_verified = True
        temp_user.save()

        # Generate a temporary JWT-scoped authentication token
        temp_token = generate_temp_token(temp_user)

        # Get initial pricing breakdown of their selected plan
        plan = temp_user.selected_plan
        billing_breakdown = build_breakdown(plan, temp_user.selected_minutes)

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

