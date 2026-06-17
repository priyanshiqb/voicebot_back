import logging
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from rest_framework_simplejwt.tokens import RefreshToken, AccessToken
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.exceptions import TokenError
from drf_spectacular.utils import extend_schema, OpenApiResponse

from utils.views.base_model_viewset import ResponseMixin
from user.models import CustomUser
from user.serializers.auth import TokenRefreshResponseSerializer
from onboard.serializers.onboarding import GeneralResponseSerializer

logger = logging.getLogger("django")

import datetime
from datetime import timezone as dt_timezone
from django.core.cache import cache
from django.utils import timezone
from django.conf import settings
from user.models.user_session import UserSession

@extend_schema(
    tags=["Auth - Access"],
    request=TokenRefreshSerializer,
    responses={
        200: OpenApiResponse(
            response=TokenRefreshResponseSerializer,
            description="Token refreshed successfully"
        ),
        400: OpenApiResponse(
            response=GeneralResponseSerializer,
            description="Invalid or missing refresh token, or inactive/deleted user account."
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

            # Check active session exists
            old_refresh_jti = token.get("jti")
            try:
                session = UserSession.objects.get(refresh_jti=old_refresh_jti, is_active=True)
            except UserSession.DoesNotExist:
                return self.error(
                    message="Session is invalid or has been logged out."
                )

            # Validate token officially
            serializer = TokenRefreshSerializer(
                data=request.data
            )

            serializer.is_valid(raise_exception=True)

            # Update session with newly rotated JTIs and expiry
            try:
                new_refresh = serializer.validated_data.get("refresh")
                new_access = serializer.validated_data.get("access")

                new_refresh_token = RefreshToken(new_refresh)
                new_access_token = AccessToken(new_access)

                new_refresh_jti = new_refresh_token.get("jti")
                new_access_jti = new_access_token.get("jti")
                
                new_refresh_iat_val = new_refresh_token.get("iat")
                new_refresh_exp_val = new_refresh_token.get("exp")
                new_access_iat_val = new_access_token.get("iat")
                new_access_exp_val = new_access_token.get("exp")
                
                new_refresh_iat = datetime.datetime.fromtimestamp(new_refresh_iat_val, tz=dt_timezone.utc) if new_refresh_iat_val else None
                new_refresh_exp = datetime.datetime.fromtimestamp(new_refresh_exp_val, tz=dt_timezone.utc) if new_refresh_exp_val else None
                new_access_iat = datetime.datetime.fromtimestamp(new_access_iat_val, tz=dt_timezone.utc) if new_access_iat_val else None
                new_access_exp = datetime.datetime.fromtimestamp(new_access_exp_val, tz=dt_timezone.utc) if new_access_exp_val else None

                new_expires_at = new_refresh_exp or (datetime.datetime.now(tz=dt_timezone.utc) + datetime.timedelta(days=1))

                # Revoke old access token from cache before assigning new one
                if session.access_jti:
                    timeout = settings.ACCESS_TOKEN_LIFETIME_SECONDS
                    if session.access_exp:
                        remaining = int(session.access_exp.timestamp() - timezone.now().timestamp())
                        timeout = max(60, remaining)
                        
                    cache.set(f"user_session:access:{session.access_jti}", "revoked", timeout=timeout)

                session.refresh_jti = new_refresh_jti
                session.access_jti = new_access_jti
                session.access_iat = new_access_iat
                session.access_exp = new_access_exp
                session.refresh_iat = new_refresh_iat
                session.refresh_exp = new_refresh_exp
                session.expires_at = new_expires_at
                session.save(update_fields=[
                    "refresh_jti", 
                    "access_jti", 
                    "access_iat", 
                    "access_exp", 
                    "refresh_iat", 
                    "refresh_exp", 
                    "expires_at"
                ])
            except Exception as rotation_err:
                logger.warning("Failed to rotate UserSession JTIs: %s", str(rotation_err))

            return self.success(
                data=serializer.validated_data,
                message="Token refreshed successfully"
            )

        except TokenError as te:
            logger.warning("TokenError during token refresh: %s", str(te))
            return self.error(
                message="Invalid refresh token."
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
