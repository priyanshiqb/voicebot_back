import logging
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from drf_spectacular.utils import extend_schema, OpenApiResponse

from utils.views.base_model_viewset import ResponseMixin
from user.serializers.auth import LogoutSerializer
from onboard.serializers.onboarding import GeneralResponseSerializer

logger = logging.getLogger("django")


@extend_schema(
    tags=["Auth - Access"],
    request=LogoutSerializer,
    responses={
        200: OpenApiResponse(
            response=GeneralResponseSerializer,
            description="Logout successful"
        ),
        400: OpenApiResponse(
            response=GeneralResponseSerializer,
            description="Invalid or missing refresh token."
        )
    }
)
class LogoutAPIView(ResponseMixin, APIView):

    def _deactivate_device(self, request):
        """Stop pushes to the device signing out, if it named itself.

        Best-effort on purpose: a logout that fails because push bookkeeping failed
        would be a worse bug than the one this fixes. But it is not optional
        behaviour — leaving the token active means the next alert lands on a device
        the user has walked away from, and notification bodies render on lock
        screens and shared browsers.

        Scoped to the caller. Naming someone else's token silently does nothing,
        exactly as ``device-tokens/deactivate/`` behaves.
        """
        device_token = (request.data.get("device_token") or "").strip()
        if not device_token or not request.user or not request.user.is_authenticated:
            return
        try:
            from core.models.device_token import DeviceToken

            DeviceToken.objects.filter(
                user=request.user, token=device_token
            ).update(is_active=False, last_error="Signed out on this device")
        except Exception as device_err:  # noqa: BLE001
            logger.warning(
                "Failed to deactivate device token on logout: %s", str(device_err)
            )

    def post(self, request):
        refresh_token = request.data.get("refresh")
        if not refresh_token:
            return self.error(
                message="Refresh token is required."
            )
        try:
            token = RefreshToken(refresh_token)
            token.blacklist()

            self._deactivate_device(request)

            # Revoke UserSession
            try:
                from user.models.user_session import UserSession
                refresh_jti = token.get("jti")
                sessions = UserSession.objects.filter(refresh_jti=refresh_jti, is_active=True)
                for session in sessions:
                    session.revoke()
            except Exception as session_err:
                logger.warning("Failed to revoke UserSession on logout: %s", str(session_err))

            return self.success(
                message="Logout successful"
            )
        except TokenError as te:
            logger.warning("TokenError during logout: %s", str(te))
            return self.error(
                message="Invalid refresh token."
            )
        except Exception as e:
            logger.exception("Error during logout: %s", str(e))
            return self.error(
                message="Invalid refresh token."
            )
