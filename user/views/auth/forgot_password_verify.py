from django.core import signing
from django.conf import settings
from django.core.cache import cache
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from drf_spectacular.utils import extend_schema, OpenApiResponse

from utils.views.base_model_viewset import ResponseMixin
from user.models import CustomUser
from user.serializers.auth import UserForgotPasswordVerifyOTPSerializer
from onboard.serializers.onboarding import GeneralResponseSerializer


@extend_schema(
    tags=["Auth - Password Reset"],
    summary="Step 2: Verify Onboarded User OTP",
    description="Verifies the emailed 6-digit OTP. Upon successful verification, invalidates the OTP and returns a secure temporary signed reset token.",
    request=UserForgotPasswordVerifyOTPSerializer,
    responses={
        200: OpenApiResponse(
            response=GeneralResponseSerializer,
            description="OTP verified successfully."
        ),
        400: OpenApiResponse(
            response=GeneralResponseSerializer,
            description="Validation or OTP error."
        )
    }
)
class UserForgotPasswordVerifyOTPAPIView(ResponseMixin, APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = UserForgotPasswordVerifyOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"].strip().lower()
        otp_code = serializer.validated_data["otp_code"]

        import hashlib
        email_hash = hashlib.sha256(email.encode('utf-8')).hexdigest()
        cache_key = f"otp_verify_attempts:{email_hash}"

        # Fetch active user
        try:
            user = CustomUser.objects.get(email=email, is_active=True, is_deleted=False)
        except CustomUser.DoesNotExist:
            # Mask user existence by incrementing/checking brute force attempts on fake key
            attempts = cache.get(cache_key, 0)
            attempts += 1
            cache.set(cache_key, attempts, 600)
            if attempts >= 5:
                return self.error(
                    message="Too many failed attempts. Verification code has been invalidated.",
                    errors=["TOO_MANY_ATTEMPTS"]
                )
            return self.error(
                message="Invalid OTP code.",
                errors=["INVALID_OTP"]
            )

        # Check brute force attempts
        attempts = cache.get(cache_key, 0)

        if attempts >= 5:
            if user.otp_code is not None:
                user.otp_code = None
                user.otp_created_at = None
                user.save(update_fields=["otp_code", "otp_created_at"])
            return self.error(
                message="Too many failed attempts. Verification code has been invalidated.",
                errors=["TOO_MANY_ATTEMPTS"]
            )

        if user.otp_code != otp_code:
            attempts += 1
            cache.set(cache_key, attempts, 600)  # 10 minutes lock duration
            if attempts >= 5:
                user.otp_code = None
                user.otp_created_at = None
                user.save(update_fields=["otp_code", "otp_created_at"])
                return self.error(
                    message="Too many failed attempts. Verification code has been invalidated.",
                    errors=["TOO_MANY_ATTEMPTS"]
                )
            return self.error(
                message="Invalid OTP code.",
                errors=["INVALID_OTP"]
            )

        if user.is_otp_expired():
            return self.error(
                message="Verification code has expired.",
                errors=["OTP_EXPIRED"]
            )

        # Invalidate OTP attempts cache upon success
        cache.delete(cache_key)

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
