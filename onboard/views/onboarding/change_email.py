from rest_framework.views import APIView
from rest_framework import status
from rest_framework.permissions import AllowAny
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings
import random

from utils.views.base_model_viewset import ResponseMixin
from onboard.models import TempUser
from onboard.serializers.onboarding import ChangeEmailSerializer, RegisterPendingResponseSerializer, GeneralResponseSerializer
from drf_spectacular.utils import extend_schema


@extend_schema(
    tags=["Onboarding - Signup Flow"],
    summary="Step 2 Helper: Change Temp User Email",
    description="Updates the email of a temporary unverified onboarding registration, generates a new 5-digit OTP, and sends it to the new email address.",
    responses={
        200: RegisterPendingResponseSerializer,
        400: GeneralResponseSerializer
    }
)
class ChangeEmailView(ResponseMixin, APIView):
    permission_classes = [AllowAny]
    serializer_class = ChangeEmailSerializer

    def post(self, request):
        serializer = ChangeEmailSerializer(data=request.data)
        if not serializer.is_valid():
            return self.error(
                message="Validation failed",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST
            )

        temp_id = serializer.validated_data["temp_registration_id"]
        new_email = serializer.validated_data["new_email"]

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

        # Ensure new_email is unique among active CustomUsers
        from django.contrib.auth import get_user_model
        User = get_user_model()
        if User.objects.filter(email=new_email).exists():
            return self.error(
                message="Validation failed",
                errors={
                    "new_email": ["An active user account is already registered with this email address."]
                },
                status_code=status.HTTP_400_BAD_REQUEST
            )

        # Ensure new_email is unique among other TempUsers
        if TempUser.objects.filter(email=new_email).exclude(id=temp_user.id).exists():
            return self.error(
                message="Validation failed",
                errors={
                    "new_email": ["A pending registration already exists for this email address."]
                },
                status_code=status.HTTP_400_BAD_REQUEST
            )

        # 1. Update the email on the TempUser record
        temp_user.email = new_email

        # 2. Generate a new 5-digit verification OTP code
        otp_code = f"{random.randint(10000, 99999)}"
        temp_user.otp_code = otp_code
        temp_user.otp_created_at = timezone.now()
        temp_user.save()

        # 3. Send the new OTP to the updated email address
        try:
            send_mail(
                subject="Verify your Voicebot Registration - Email Updated",
                message=f"Your email has been updated. Your verification code is: {otp_code}. It is valid for 10 minutes.",
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@voicebot.com"),
                recipient_list=[new_email],
                fail_silently=False
            )
        except Exception as e:
            print(f"OTP Email delivery skipped/failed: {e}. OTP Code for {new_email} is: {otp_code}")

        return self.success(
            data={
                "temp_registration_id": str(temp_user.id),
                "email": temp_user.email
            },
            message="Email updated successfully. A new verification OTP has been emailed.",
            status_code=status.HTTP_200_OK
        )

