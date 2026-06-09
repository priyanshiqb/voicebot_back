import logging
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
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

    def post(self, request):
        refresh_token = request.data.get("refresh")
        if not refresh_token:
            return self.error(
                message="Refresh token is required."
            )
        try:
            token = RefreshToken(refresh_token)
            token.blacklist()

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
        except Exception as e:
            logger.exception("Error during logout: %s", str(e))
            return self.error(
                message="Invalid refresh token."
            )
