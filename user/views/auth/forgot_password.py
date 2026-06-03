import random
import logging
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from drf_spectacular.utils import extend_schema, OpenApiResponse

from utils.views.base_model_viewset import ResponseMixin
from user.models import CustomUser
from user.serializers.auth import UserForgotPasswordSerializer

logger = logging.getLogger("django")


@extend_schema(
    tags=["Auth - Password Reset"],
    summary="Step 1: Forgot Onboarded User Password",
    description="Initiates password reset for active onboarded users by emailing a 6-digit verification OTP valid for 2 minutes.",
    request=UserForgotPasswordSerializer,
    responses={
        200: OpenApiResponse(description="OTP sent successfully"),
        400: OpenApiResponse(description="Validation error")
    }
)
class UserForgotPasswordAPIView(ResponseMixin, APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = UserForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        email = serializer.validated_data["email"].strip().lower()

        try:
            user = CustomUser.objects.get(email=email, is_active=True, is_deleted=False)
        except CustomUser.DoesNotExist:
            return self.success(
                message="If the email is registered, a verification OTP code will be sent shortly."
            )

        # Generate a secure 6-digit numeric OTP code
        otp_code = f"{random.randint(100000, 999999)}"
        user.otp_code = otp_code
        user.otp_created_at = timezone.now()
        user.save(update_fields=["otp_code", "otp_created_at"])

        # Email the OTP code
        try:
            send_mail(
                subject="Reset your Voicebot Account Password - OTP",
                message=f"You requested to reset your password. Your verification OTP code is: {otp_code}. It is valid for 2 minutes.",
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@voicebot.com"),
                recipient_list=[user.email],
                fail_silently=False
            )
        except Exception as e:
            logger.exception("Password reset active user OTP email delivery skipped/failed: %s", str(e))

        return self.success(
            data={
                "user_id": str(user.id)
            },
            message="If the email is registered, a verification OTP code will be sent shortly."
        )
