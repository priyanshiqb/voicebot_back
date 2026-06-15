from rest_framework.views import APIView
from rest_framework import status
from rest_framework.permissions import AllowAny
from utils.tasks import send_email_task
from django.conf import settings
from django.core import signing
from django.utils import timezone
import random

from utils.views.base_model_viewset import ResponseMixin
from onboard.models import TempUser
from onboard.serializers.onboarding import ForgotPasswordSerializer, GeneralResponseSerializer
from drf_spectacular.utils import extend_schema


@extend_schema(
    tags=["Onboarding - Password Reset"],
    summary="Step 1: Forgot Temp User Password",
    description="Initiates password reset for unverified onboarding temporary users by emailing a 6-digit OTP verification code valid for 2 minutes.",
    responses={
        200: GeneralResponseSerializer,
        400: GeneralResponseSerializer
    }
)
class ForgotPasswordView(ResponseMixin, APIView):
    permission_classes = [AllowAny]
    serializer_class = ForgotPasswordSerializer

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        if not serializer.is_valid():
            return self.error(
                message="Validation failed",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST
            )

        email = serializer.validated_data["email"].strip().lower()

        try:
            temp_user = TempUser.objects.get(email=email)
        except TempUser.DoesNotExist:
            return self.success(
                message="If the email is registered in our temporary signup system, a verification OTP code will be sent shortly.",
                status_code=status.HTTP_200_OK
            )

        # 1. Generate a secure 6-digit numeric OTP code
        otp_code = f"{random.randint(100000, 999999)}"
        temp_user.otp_code = otp_code
        temp_user.otp_created_at = timezone.now()
        temp_user.save()

        # 2. Email the OTP code via Celery task
        try:
            send_email_task.delay(
                subject="Reset your Voicebot Onboarding Password - OTP",
                message=f"You requested to reset your password during onboarding. Your verification OTP code is: {otp_code}. It is valid for 2 minutes.",
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@voicebot.com"),
                recipient_list=[temp_user.email],
                fail_silently=False,
                title="Reset Password Request",
                lead_text="We received a request to reset your onboarding password. Please use the verification code below to authorize this change.",
                otp_code=otp_code,
                body_text="This code is valid for 2 minutes. If you did not request a password reset, please secure your account immediately."
            )
        except Exception as e:
            print(f"Password reset OTP email queuing skipped/failed: {e}. OTP Code: {otp_code}")

        return self.success(
            data={
                "temp_registration_id": str(temp_user.id)
            },
            message="If the email is registered in our temporary signup system, a verification OTP code will be sent shortly.",
            status_code=status.HTTP_200_OK
        )


