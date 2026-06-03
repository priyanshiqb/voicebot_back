from django.core import signing
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from drf_spectacular.utils import extend_schema, OpenApiResponse

from utils.views.base_model_viewset import ResponseMixin
from user.models import CustomUser
from user.serializers.auth import UserForgotPasswordVerifyOTPSerializer


@extend_schema(
    tags=["Auth - Password Reset"],
    summary="Step 2: Verify Onboarded User OTP",
    description="Verifies the emailed 6-digit OTP. Upon successful verification, invalidates the OTP and returns a secure temporary signed reset token.",
    request=UserForgotPasswordVerifyOTPSerializer,
    responses={
        200: OpenApiResponse(description="OTP verified successfully"),
        400: OpenApiResponse(description="Validation or OTP error")
    }
)
class UserForgotPasswordVerifyOTPAPIView(ResponseMixin, APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = UserForgotPasswordVerifyOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user_id = serializer.validated_data["user_id"]
        otp_code = serializer.validated_data["otp_code"]

        try:
            user = CustomUser.objects.get(id=user_id, is_active=True, is_deleted=False)
        except CustomUser.DoesNotExist:
            return self.error(
                message="Invalid user ID.",
                errors=["USER_NOT_FOUND"]
            )

        if user.otp_code != otp_code:
            return self.error(
                message="Invalid OTP code.",
                errors=["INVALID_OTP"]
            )

        if user.is_otp_expired():
            return self.error(
                message="Verification code has expired.",
                errors=["OTP_EXPIRED"]
            )

        # Invalidate OTP code in the database
        user.otp_code = None
        user.otp_created_at = None
        user.save(update_fields=["otp_code", "otp_created_at"])

        # Generate a temporary signed reset token valid for 30 minutes (1800 seconds)
        token = signing.dumps({
            "user_id": str(user.id),
            "password_hash": user.password
        }, key=settings.SECRET_KEY)

        return self.success(
            data={
                "token": token
            },
            message="OTP verified successfully."
        )
