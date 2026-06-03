import logging
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from drf_spectacular.utils import extend_schema, OpenApiResponse

from utils.views.base_model_viewset import ResponseMixin
from user.serializers.auth import LogoutSerializer

logger = logging.getLogger("django")


@extend_schema(
    tags=["Auth - Access"],
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
