from rest_framework.views import APIView
from rest_framework import status
from rest_framework.permissions import AllowAny
from django.utils import timezone
from utils.tasks import send_email_task
from django.conf import settings
import random

from utils.views.base_model_viewset import ResponseMixin
from onboard.serializers.onboarding import TempUserSignupSerializer, RegisterPendingResponseSerializer, GeneralResponseSerializer
from drf_spectacular.utils import extend_schema


@extend_schema(
    tags=["Onboarding - Signup Flow"],
    summary="Step 1: User Signup Registration",
    description="Captures initial signup details (name, email, business name, password, optional referral code) and emails a 6-digit OTP code.",
    responses={
        201: RegisterPendingResponseSerializer,
        400: GeneralResponseSerializer
    }
)
class TempUserRegisterView(ResponseMixin, APIView):
    permission_classes = [AllowAny]
    serializer_class = TempUserSignupSerializer

    def post(self, request):
        serializer = TempUserSignupSerializer(data=request.data)
        if not serializer.is_valid():
            return self.error(
                message="Validation failed",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST
            )

        temp_user = serializer.save()

        # Generate a random 6-digit verification OTP code
        otp_code = f"{random.randint(100000, 999999)}"
        temp_user.otp_code = otp_code
        temp_user.otp_created_at = timezone.now()
        temp_user.save()

        # Send the verification code via email via Celery task
        try:
            send_email_task.delay(
                subject="Verify your Voicebot Registration",
                message=f"Your verification code is: {otp_code}. It is valid for {settings.OTP_VALIDITY_MINUTES} minutes.",
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@voicebot.com"),
                recipient_list=[temp_user.email],
                fail_silently=False,
                title="Verify Your Registration",
                lead_text="Thank you for signing up with Voicebot. Please use the verification code below to confirm your registration.",
                otp_code=otp_code,
                body_text=f"This code is valid for {settings.OTP_VALIDITY_MINUTES} minutes. If you did not sign up for an account, please disregard this email."
            )
        except Exception as e:
            # Safe fallback log in development/testing environments
            print(f"OTP Email queuing skipped/failed: {e}. OTP Code for {temp_user.email} is: {otp_code}")

        return self.success(
            data={
                "temp_registration_id": str(temp_user.id),
                "email": temp_user.email
            },
            message="Registration initiated. Verification OTP has been emailed.",
            status_code=status.HTTP_201_CREATED
        )

