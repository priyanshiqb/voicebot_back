from rest_framework.views import APIView
from rest_framework import status
from rest_framework.permissions import AllowAny
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings
import random

from utils.views.base_model_viewset import ResponseMixin
from onboard.models import TempUser
from onboard.serializers.onboarding import ResendOTPSerializer, RegisterPendingResponseSerializer, GeneralResponseSerializer
from drf_spectacular.utils import extend_schema


@extend_schema(
    tags=["Onboarding - Signup Flow"],
    summary="Step 2 Helper: Resend Verification OTP",
    description="Resends a new 6-digit OTP to the registrant's email, subject to a 30-second cooldown period.",
    responses={
        200: RegisterPendingResponseSerializer,
        400: GeneralResponseSerializer
    }
)
class ResendOTPView(ResponseMixin, APIView):
    permission_classes = [AllowAny]
    serializer_class = ResendOTPSerializer

    def post(self, request):
        temp_id = request.data.get("temp_registration_id")

        try:
            temp_user = TempUser.objects.get(id=temp_id)
        except (TempUser.DoesNotExist, ValueError):
            return self.error(
                message="Invalid registration ID",
                errors=["REGISTRATION_NOT_FOUND"],
                status_code=status.HTTP_400_BAD_REQUEST
            )

        if temp_user.is_email_verified:
            return self.error(
                message="Email is already verified",
                errors=["EMAIL_ALREADY_VERIFIED"],
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

        # Generate a new 6-digit verification OTP code
        otp_code = f"{random.randint(100000, 999999)}"
        temp_user.otp_code = otp_code
        temp_user.otp_created_at = now
        temp_user.save()

        # Send the verification code via email
        try:
            send_mail(
                subject="Verify your Voicebot Registration - New OTP",
                message=f"Your new verification code is: {otp_code}. It is valid for 10 minutes.",
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@voicebot.com"),
                recipient_list=[temp_user.email],
                fail_silently=False
            )
        except Exception as e:
            print(f"OTP Email delivery skipped/failed: {e}. OTP Code is: {otp_code}")

        return self.success(
            data={
                "temp_registration_id": str(temp_user.id),
                "email": temp_user.email
            },
            message="A new verification OTP has been emailed.",
            status_code=status.HTTP_200_OK
        )

