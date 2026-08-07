from rest_framework.views import APIView
from rest_framework import status
from rest_framework.permissions import AllowAny
from django.utils import timezone
from utils.tasks import send_email_task
from user.models.email_delivery_log import EmailDeliveryLog
from user.services import email_log
from django.conf import settings
import random

from utils.views.base_model_viewset import ResponseMixin
from onboard.models import TempUser
from onboard.serializers.onboarding import ForgotPasswordResendOTPSerializer, GeneralResponseSerializer
from drf_spectacular.utils import extend_schema


@extend_schema(
    tags=["Onboarding - Password Reset"],
    summary="Step 1 Helper: Resend Forgot Password OTP",
    description="Resends a new 6-digit OTP to the temporary user's email, subject to a 30-second cooldown period.",
    responses={
        200: GeneralResponseSerializer,
        400: GeneralResponseSerializer
    }
)
class ForgotPasswordResendOTPView(ResponseMixin, APIView):
    permission_classes = [AllowAny]
    serializer_class = ForgotPasswordResendOTPSerializer

    def post(self, request):
        serializer = ForgotPasswordResendOTPSerializer(data=request.data)
        if not serializer.is_valid():
            return self.error(
                message="Validation failed",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST
            )

        temp_id = serializer.validated_data["temp_registration_id"]

        try:
            temp_user = TempUser.objects.get(id=temp_id)
        except (TempUser.DoesNotExist, ValueError):
            return self.error(
                message="Invalid registration ID",
                errors=["REGISTRATION_NOT_FOUND"],
                status_code=status.HTTP_400_BAD_REQUEST
            )

        # Resend OTP cooldown calculation (30 seconds limit)
        now = timezone.now()
        if temp_user.otp_created_at:
            elapsed = (now - temp_user.otp_created_at).total_seconds()
            if elapsed < 30:
                cooldown_remaining = int(30 - elapsed)
                return self.error(
                    message="Please wait before resending OTP.",
                    data={"cooldown_remaining": cooldown_remaining},
                    errors=["OTP_COOLDOWN_ACTIVE"],
                    status_code=status.HTTP_400_BAD_REQUEST
                )

        # Generate a new secure 6-digit verification OTP code
        otp_code = f"{random.randint(100000, 999999)}"
        temp_user.otp_code = otp_code
        temp_user.otp_created_at = now
        temp_user.save()

        # Send the verification code via email via Celery task
        try:
            delivery_log_id = email_log.record(
                EmailDeliveryLog.Purpose.PASSWORD_RESET,
                temp_user.email,
                request=request,
                temp_user_id=temp_user.id,
            )
            send_email_task.delay(
                delivery_log_id=delivery_log_id,
                subject="Reset your Voicebot Onboarding Password - New OTP",
                message=f"You requested to reset your password during onboarding. Your new verification OTP code is: {otp_code}. It is valid for {settings.OTP_VALIDITY_MINUTES} minutes.",
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@voicebot.com"),
                recipient_list=[temp_user.email],
                fail_silently=False,
                title="New Verification Code",
                lead_text="We received a request to resend your password reset code. Please use the verification code below to authorize this change.",
                otp_code=otp_code,
                body_text=f"This code is valid for {settings.OTP_VALIDITY_MINUTES} minutes. If you did not request a password reset, please secure your account immediately."
            )
        except Exception as e:
            print(f"ForgotPassword OTP Email queuing skipped/failed: {e}. OTP Code is: {otp_code}")

        return self.success(
            data={
                "temp_registration_id": str(temp_user.id),
                "email": temp_user.email
            },
            message="A new verification OTP has been emailed.",
            status_code=status.HTTP_200_OK
        )

