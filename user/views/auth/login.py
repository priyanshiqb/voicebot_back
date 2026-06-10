from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from rest_framework_simplejwt.tokens import RefreshToken
from drf_spectacular.utils import extend_schema, OpenApiResponse
import logging

logger = logging.getLogger("django")

from utils.views.base_model_viewset import ResponseMixin
from user.serializers.auth import LoginSerializer, LoginResponseSerializer
from user.serializers.custom_user import UserRefSerializer
from onboard.serializers.onboarding import GeneralResponseSerializer

import datetime
from datetime import timezone as dt_timezone
from user.models.user_session import UserSession
from utils.device_utils import parse_user_agent, get_client_ip

@extend_schema(
    tags=["Auth - Access"],
    request=LoginSerializer,
    responses={
        200: OpenApiResponse(
            response=LoginResponseSerializer,
            description="Login successful"
        ),
        400: OpenApiResponse(
            response=GeneralResponseSerializer,
            description="Validation failed, invalid email/password."
        ),
        403: OpenApiResponse(
            response=GeneralResponseSerializer,
            description="Account or IP locked due to too many failed login attempts."
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

        # Create active UserSession record
        try:
            refresh_jti = refresh.get("jti")
            access_jti = access.get("jti")
            
            refresh_iat_val = refresh.get("iat")
            refresh_exp_val = refresh.get("exp")
            access_iat_val = access.get("iat")
            access_exp_val = access.get("exp")
            
            refresh_iat = datetime.datetime.fromtimestamp(refresh_iat_val, tz=dt_timezone.utc) if refresh_iat_val else None
            refresh_exp = datetime.datetime.fromtimestamp(refresh_exp_val, tz=dt_timezone.utc) if refresh_exp_val else None
            access_iat = datetime.datetime.fromtimestamp(access_iat_val, tz=dt_timezone.utc) if access_iat_val else None
            access_exp = datetime.datetime.fromtimestamp(access_exp_val, tz=dt_timezone.utc) if access_exp_val else None
            
            expires_at = refresh_exp or (datetime.datetime.now(tz=dt_timezone.utc) + datetime.timedelta(days=1))

            UserSession.objects.create(
                user=user,
                refresh_jti=refresh_jti,
                access_jti=access_jti,
                access_iat=access_iat,
                access_exp=access_exp,
                refresh_iat=refresh_iat,
                refresh_exp=refresh_exp,
                device_name=parse_user_agent(request.headers.get("User-Agent")),
                ip_address=get_client_ip(request),
                expires_at=expires_at
            )
        except Exception as session_err:
            logger.warning("Failed to create UserSession: %s", str(session_err))

        return self.success(
            data={
                "access": str(access),
                "refresh": str(refresh),
                "user": UserRefSerializer(user).data
            },
            message="Login successful"
        )
