from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from drf_spectacular.utils import extend_schema, OpenApiResponse

from tenant.common.views.agent_tenant_scope import VBAgentActionPermission
from utils.views.base_model_viewset import BaseViewSet
from user.models.user_session import UserSession
from user.serializers.session import (
    UserSessionSerializer,
    UserSessionListSerializer,
    UserSessionDropdownSerializer,
)
from onboard.serializers.onboarding import GeneralResponseSerializer

@extend_schema(tags=["Auth / Session"])
class UserSessionViewSet(BaseViewSet):
    """
    ViewSet to manage user sessions/devices.
    """
    # VBAgentActionPermission restated because assigning permission_classes here would
    # otherwise drop the one BaseViewSet sets, silently reopening this to machine
    # callers. No `vb_agent_allowed_actions`, so they are denied.
    permission_classes = (IsAuthenticated, VBAgentActionPermission)
    http_method_names = ["get", "delete", "post", "options", "head"]

    # Explicit properties as required by AGENTS.md guidelines
    queryset = UserSession.objects.all()
    serializer_class = UserSessionSerializer
    list_serializer_class = UserSessionListSerializer
    dropdown_serializer_class = UserSessionDropdownSerializer
    filterset_fields = ["is_active"]
    search_fields = ["device_name", "ip_address"]
    ordering_fields = ["created_at", "updated_at"]
    ordering = ["-updated_at"]
    select_related_fields = ["user"]
    prefetch_related_fields = []

    def get_queryset(self):
        # Only return active, unexpired sessions for the logged-in user
        return super().get_queryset().filter(
            user=self.request.user,
            is_active=True,
            expires_at__gt=timezone.now()
        )

    def perform_destroy(self, instance):
        instance.revoke()

    @extend_schema(
        request=None,
        responses={
            200: OpenApiResponse(
                response=GeneralResponseSerializer,
                description="All other sessions revoked successfully"
            )
        }
    )
    @action(detail=False, methods=["post"], url_path="revoke-all")
    def revoke_all(self, request):
        """
        Revoke all other active sessions except the current one.
        """
        current_auth = getattr(request, "auth", None)
        current_jti = current_auth.get("jti") if (current_auth and hasattr(current_auth, "get")) else None

        sessions = self.get_queryset()
        count = 0
        for session in sessions:
            if session.access_jti != current_jti:
                session.revoke()
                count += 1

        return self.success(message=f"Successfully signed out of {count} other device(s).")
