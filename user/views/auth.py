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