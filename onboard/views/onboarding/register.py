from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings
import random

from onboard.serializers.onboarding import TempUserSignupSerializer, RegisterPendingResponseSerializer, GeneralResponseSerializer
from drf_spectacular.utils import extend_schema


@extend_schema(
    tags=["Onboarding - Signup Flow"],
    summary="Step 1: User Signup Registration",
    description="Captures initial signup details (name, email, business name, password, optional referral code) and emails a 5-digit OTP code.",
    responses={
        201: RegisterPendingResponseSerializer,
        400: GeneralResponseSerializer
    }
)
class TempUserRegisterView(APIView):
    permission_classes = [AllowAny]
    serializer_class = TempUserSignupSerializer

    def post(self, request):
        serializer = TempUserSignupSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                "status": False,
                "message": "Validation failed",
                "data": None,
                "errors": serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        temp_user = serializer.save()

        # Generate a random 5-digit verification OTP code
        otp_code = f"{random.randint(10000, 99999)}"
        temp_user.otp_code = otp_code
        temp_user.otp_created_at = timezone.now()
        temp_user.save()

        # Send the verification code via email
        try:
            send_mail(
                subject="Verify your Voicebot Registration",
                message=f"Your verification code is: {otp_code}. It is valid for 10 minutes.",
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@voicebot.com"),
                recipient_list=[temp_user.email],
                fail_silently=False
            )
        except Exception as e:
            # Safe fallback log in development/testing environments
            print(f"OTP Email delivery skipped/failed: {e}. OTP Code for {temp_user.email} is: {otp_code}")

        return Response({
            "status": True,
            "message": "Registration initiated. Verification OTP has been emailed.",
            "data": {
                "temp_registration_id": str(temp_user.id),
                "email": temp_user.email
            },
            "errors": None
        }, status=status.HTTP_201_CREATED)
