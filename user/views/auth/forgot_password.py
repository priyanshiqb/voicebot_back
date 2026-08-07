import random
import logging
from django.utils import timezone
from utils.tasks import send_email_task
from user.models.email_delivery_log import EmailDeliveryLog
from user.services import email_log
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from drf_spectacular.utils import extend_schema, OpenApiResponse

from utils.views.base_model_viewset import ResponseMixin
from user.models import CustomUser
from user.serializers.auth import UserForgotPasswordSerializer
from onboard.serializers.onboarding import GeneralResponseSerializer

logger = logging.getLogger("django")


@extend_schema(
    tags=["Auth - Password Reset"],
    summary="Step 1: Forgot Onboarded User Password",
    description="Initiates password reset for active onboarded users by emailing a 6-digit verification OTP valid for 2 minutes.",
    request=UserForgotPasswordSerializer,
    responses={
        200: OpenApiResponse(
            response=GeneralResponseSerializer,
            description="OTP generation flow initiated successfully."
        ),
        400: OpenApiResponse(
            response=GeneralResponseSerializer,
            description="Validation error."
        )
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

        # OTP cooldown check (30 seconds limit)
        now = timezone.now()
        if user.otp_created_at:
            elapsed = (now - user.otp_created_at).total_seconds()
            if elapsed < 30:
                cooldown_remaining = int(30 - elapsed)
                return self.error(
                    message="Please wait before requesting another OTP.",
                    data={"cooldown_remaining": cooldown_remaining},
                    errors=["OTP_COOLDOWN_ACTIVE"]
                )

        # Generate a secure 6-digit numeric OTP code
        otp_code = f"{random.randint(100000, 999999)}"
        user.otp_code = otp_code
        user.otp_created_at = timezone.now()
        user.save(update_fields=["otp_code", "otp_created_at"])

        # Email the OTP code via Celery task
        try:
            delivery_log_id = email_log.record(
                EmailDeliveryLog.Purpose.PASSWORD_RESET,
                user.email,
                request=request,
                user=user,
                tenant_id=getattr(
                    getattr(user, "tenant_mapping", None), "tenant_id", None
                ),
            )
            send_email_task.delay(
                delivery_log_id=delivery_log_id,
                subject="Reset your Voicebot Account Password - OTP",
                message=f"You requested to reset your password. Your verification OTP code is: {otp_code}. It is valid for {settings.OTP_VALIDITY_MINUTES} minutes.",
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@voicebot.com"),
                recipient_list=[user.email],
                fail_silently=False,
                title="Reset Password Request",
                lead_text="We received a request to reset your password. Please use the verification code below to authorize this change.",
                otp_code=otp_code,
                body_text=f"This code is valid for {settings.OTP_VALIDITY_MINUTES} minutes. If you did not request a password reset, please secure your account immediately."
            )
        except Exception as e:
            logger.exception("Password reset active user OTP email queuing skipped/failed: %s", str(e))

        return self.success(
            message="If the email is registered, a verification OTP code will be sent shortly."
        )
