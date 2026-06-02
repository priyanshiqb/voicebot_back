from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from rest_framework_simplejwt.tokens import (
    RefreshToken
)
from user.serializers.auth import (
    LoginSerializer,
    LoginResponseSerializer,
    LogoutSerializer,
    TokenRefreshResponseSerializer
)
from utils.views.base_model_viewset import ResponseMixin
from user.serializers.custom_user import (
    UserRefSerializer
)
from rest_framework_simplejwt.serializers import (
    TokenRefreshSerializer
)
from drf_spectacular.utils import (
    extend_schema,
    OpenApiResponse,
)
import logging
logger = logging.getLogger("django")
from user.models import CustomUser
from django.utils import timezone

@extend_schema(
    tags=["Authentication"],
    request=LoginSerializer,
    responses={
        200: OpenApiResponse(
            response=LoginResponseSerializer,
            description="Login successful"
        )
    }
)
class LoginAPIView(ResponseMixin, APIView):

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        from defender.utils import is_user_already_locked, is_source_ip_already_locked, get_ip

        email = request.data.get("email", "").strip().lower()
        ip = get_ip(request)

        if is_user_already_locked(email) or is_source_ip_already_locked(ip):
            return self.error(
                message="Account or IP temporarily locked due to too many failed login attempts. Please try again in 1 hour.",
                status_code=403
            )

        serializer = LoginSerializer(
            data=request.data
        )

        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])
        refresh = RefreshToken.for_user(user)
        access = refresh.access_token

        return self.success(
            data={
                "access": str(access),
                "refresh": str(refresh),
                "user": UserRefSerializer(user).data
            },
            message="Login successful"
        )

@extend_schema(
    tags=["Authentication"],
    request=LogoutSerializer,
    responses={
        200: OpenApiResponse(
            description="Logout successful"
        )
    }
)
class LogoutAPIView(ResponseMixin, APIView):

    def post(self, request):
        refresh_token = request.data.get("refresh")
        if not refresh_token:
            return self.error(
                message="Refresh token is required."
            )
        try:
            token = RefreshToken(refresh_token)
            token.blacklist()

            return self.success(
                message="Logout successful"
            )
        except Exception as e:
            logger.exception("Error during logout: %s", str(e))
            return self.error(
                message="Invalid refresh token."
            )

@extend_schema(
    tags=["Authentication"],
    request=TokenRefreshSerializer,
    responses={
        200: OpenApiResponse(
            response=TokenRefreshResponseSerializer,
            description="Token refreshed successfully"
        )
    }
)       
class TokenRefreshAPIView(ResponseMixin, APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        refresh_token = request.data.get("refresh")

        if not refresh_token:
            return self.error(
                message="Refresh token is required."
            )
        
        try:
            token = RefreshToken(refresh_token)

            user_id = token.get("user_id")

            user = CustomUser.objects.get(id=user_id)

            # User status checks
            if not user.is_active:
                return self.error(
                    message="User account is inactive."
                )

            if user.is_deleted:
                return self.error(
                    message="User account is deleted."
                )

            # Validate token officially
            serializer = TokenRefreshSerializer(
                data=request.data
            )

            serializer.is_valid(raise_exception=True)

            return self.success(
                data=serializer.validated_data,
                message="Token refreshed successfully"
            )

        except CustomUser.DoesNotExist:
            return self.error(
                message="User not found."
            )

        except Exception as e:
            logger.exception(
                "Error during token refresh: %s",
                str(e)
            )

            return self.error(
                message="Invalid refresh token."
            )


from django.core import signing
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.mail import send_mail
import random
from user.serializers.auth import (
    UserForgotPasswordSerializer,
    UserForgotPasswordVerifyOTPSerializer,
    UserResetPasswordSerializer
)

@extend_schema(
    tags=["Reset Onboarded User Password"],
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


@extend_schema(
    tags=["Reset Onboarded User Password"],
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


@extend_schema(
    tags=["Reset Onboarded User Password"],
    summary="Step 3: Reset Onboarded User Password",
    description="Accepts the temporary signed reset token and user ID to perform password updates, guaranteeing single-use token lifecycle.",
    request=UserResetPasswordSerializer,
    responses={
        200: OpenApiResponse(description="Password reset successfully"),
        400: OpenApiResponse(description="Validation or Token error")
    }
)
class UserResetPasswordAPIView(ResponseMixin, APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = UserResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user_id = str(serializer.validated_data["user_id"])
        token = serializer.validated_data["token"]
        new_password = serializer.validated_data["password"]

        try:
            user = CustomUser.objects.get(id=user_id, is_active=True, is_deleted=False)
        except CustomUser.DoesNotExist:
            return self.error(
                message="Invalid user ID.",
                errors=["USER_NOT_FOUND"]
            )

        # Cryptographically verify the signed token
        try:
            payload = signing.loads(token, key=settings.SECRET_KEY, max_age=1800)
        except signing.SignatureExpired:
            return self.error(
                message="Reset token has expired.",
                errors=["TOKEN_EXPIRED"]
            )
        except signing.BadSignature:
            return self.error(
                message="Invalid token signature.",
                errors=["INVALID_TOKEN"]
            )

        # Ensure the token belongs to the specified user
        if payload.get("user_id") != user_id:
            return self.error(
                message="Token is not associated with this user.",
                errors=["INVALID_USER_TOKEN"]
            )

        # Ensure the token has not already been used (one-time usability scope check)
        if payload.get("password_hash") != user.password:
            return self.error(
                message="This reset link has already been used.",
                errors=["TOKEN_ALREADY_USED"]
            )

        # Perform password update
        user.set_password(new_password)
        user.save()

        return self.success(
            message="Password reset successfully."
        )