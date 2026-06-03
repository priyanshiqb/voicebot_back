from rest_framework.views import APIView
from rest_framework import status
from rest_framework.permissions import AllowAny
from django.conf import settings
from django.core import signing

from utils.views.base_model_viewset import ResponseMixin
from onboard.models import TempUser
from onboard.serializers.onboarding import ForgotPasswordVerifyOTPSerializer, GeneralResponseSerializer
from drf_spectacular.utils import extend_schema


@extend_schema(
    tags=["Onboarding - Password Reset"],
    summary="Step 2: Verify Forgot Password OTP",
    description="Verifies the emailed 6-digit OTP. Upon successful verification, invalidates the OTP and returns a secure temporary signed reset token.",
    responses={
        200: GeneralResponseSerializer,
        400: GeneralResponseSerializer
    }
)
class ForgotPasswordVerifyOTPView(ResponseMixin, APIView):
    permission_classes = [AllowAny]
    serializer_class = ForgotPasswordVerifyOTPSerializer

    def post(self, request):
        serializer = ForgotPasswordVerifyOTPSerializer(data=request.data)
        if not serializer.is_valid():
            return self.error(
                message="Validation failed",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST
            )

        temp_id = serializer.validated_data["temp_registration_id"]
        otp_code = serializer.validated_data["otp_code"]

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

        # Invalidate the OTP code in the database
        temp_user.otp_code = None
        temp_user.otp_created_at = None
        temp_user.save()

        # Generate a temporary signed reset token valid for 30 minutes (1800 seconds)
        token = signing.dumps({
            "temp_user_id": str(temp_user.id),
            "password_hash": temp_user.password_hash
        }, key=settings.SECRET_KEY)

        return self.success(
            data={
                "token": token
            },
            message="OTP verified successfully",
            status_code=status.HTTP_200_OK
        )

