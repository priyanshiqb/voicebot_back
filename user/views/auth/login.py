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
from django.db import transaction
from user.services import sessions as session_service

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
        ),
        503: OpenApiResponse(
            response=GeneralResponseSerializer,
            description=(
                "Credentials were correct but the session could not be registered, so "
                "no tokens are issued. Every authenticated request resolves its access "
                "token against that session row — returning tokens without one would "
                "mean a login that 401s on every call. Safe to retry."
            )
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
        refresh = RefreshToken.for_user(user)

        # Override expiration based on user's custom timeout (in hours)
        timeout_hours = getattr(user, 'session_timeout_hours', 168)
        refresh.set_exp(lifetime=datetime.timedelta(hours=max(1, timeout_hours)))

        access = refresh.access_token

        # The session row is part of the login, not bookkeeping alongside it. Every
        # authenticated request resolves its access token against this row, so tokens
        # issued without one are dead on arrival — every call 401s.
        #
        # This used to swallow the failure and return the tokens anyway, which turned a
        # transient database blip into "login succeeded, nothing works" with only a
        # warning in the logs to explain it. Failing loudly gives the client something
        # it can retry.
        #
        # Atomic with the last_login stamp so the two cannot half-apply.
        try:
            with transaction.atomic():
                user.last_login = timezone.now()
                user.save(update_fields=["last_login"])
                session_service.create_for_tokens(user, request, refresh, access)
        except Exception:
            logger.exception("Failed to create UserSession during login for %s", user.pk)
            return self.error(
                message="Could not start a session. Please try again.",
                status_code=503,
            )

        return self.success(
            data={
                "access": str(access),
                "refresh": str(refresh),
                "user": UserRefSerializer(user).data
            },
            message="Login successful"
        )
