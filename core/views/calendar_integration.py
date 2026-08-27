import logging
import secrets

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.shortcuts import redirect
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny
from drf_spectacular.utils import extend_schema

from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from tenant.models.Tenant import Tenant
from core.models.calendar_integration import CalendarIntegration
from core.serializers.calendar_integration import (
    CalendarIntegrationDropdownSerializer,
    CalendarIntegrationListSerializer,
    CalendarIntegrationSerializer,
)

logger = logging.getLogger(__name__)

_STATE_SALT = "scheduling.calendar.oauth"
_STATE_MAX_AGE = 600  # seconds

# Providers that once existed and may still be sent by an old client build.
_RETIRED_PROVIDERS = {"OUTLOOK"}

from drf_spectacular.utils import extend_schema

@extend_schema(tags=["Calendar Integration"])
class CalendarIntegrationViewSet(TenantBaseViewSet):
    """
    Manage external-calendar connections. Integrations are created via the OAuth
    flow (connect -> Google consent -> callback), listed/inspected read-only, and
    removed via DELETE (which revokes the grant). Direct create/update is disabled.
    """

    queryset = CalendarIntegration.objects.all()
    serializer_class = CalendarIntegrationSerializer
    list_serializer_class = CalendarIntegrationListSerializer
    dropdown_serializer_class = CalendarIntegrationDropdownSerializer
    http_method_names = ["get", "delete", "head", "options"]

    filterset_fields = ["user", "provider", "sync_status"]
    ordering_fields = ["created_at"]
    ordering = ["-created_at"]
    select_related_fields = ["user"]

    def get_authenticators(self):
        # The OAuth callback is hit by Google's browser redirect, which cannot
        # carry our auth header — it is trusted via the signed `state` instead.
        if getattr(self, "action", None) == "callback" or "callback" in getattr(self, "action_map", {}).values():
            return []
        return super().get_authenticators()

    def get_permissions(self):
        if getattr(self, "action", None) == "callback" or "callback" in getattr(self, "action_map", {}).values():
            return [AllowAny()]
        return super().get_permissions()

    def _tenant(self, request):
        try:
            return request.user.tenant_mapping.tenant
        except Exception:
            raise PermissionDenied(
                "Your user account is not associated with any active tenant."
            )

    def _resolve_provider(self, value):
        """Normalize/validate a provider name; returns a Provider value or None."""
        provider = (value or CalendarIntegration.Provider.GOOGLE).upper()
        if provider not in CalendarIntegration.Provider.values:
            return None
        return provider

    @staticmethod
    def _unsupported_provider_message(value):
        """Retired providers get a named answer rather than a generic rejection, so a
        stale client build shows something actionable."""
        if (value or "").upper() in _RETIRED_PROVIDERS:
            return "Outlook is no longer supported. Only GOOGLE is available."
        return "Unsupported calendar provider."

    @action(detail=False, methods=["get"])
    def connect(self, request):
        """Return the provider consent URL to start connecting a calendar.

        `?provider=GOOGLE` (the default, and currently the only option)."""
        from core.services import calendars

        if not settings.SCHEDULING_TOKEN_ENCRYPTION_KEY:
            return self.error(message="Missing SCHEDULING_TOKEN_ENCRYPTION_KEY config.", status_code=500)

        requested = request.query_params.get("provider")
        provider = self._resolve_provider(requested)
        if provider is None:
            return self.error(
                message=self._unsupported_provider_message(requested), status_code=400
            )

        tenant = self._tenant(request)
        nonce = secrets.token_hex(16)
        state = signing.dumps(
            {
                "user_id": str(request.user.id),
                "tenant_id": str(tenant.id),
                "provider": provider,
                "nonce": nonce,
            },
            salt=_STATE_SALT,
        )
        cache.set(f"oauth_state_{nonce}", True, timeout=_STATE_MAX_AGE)
        try:
            auth_url = calendars.get_provider_class(provider).authorization_url(state)
        except Exception:  # noqa: BLE001
            logger.exception("authorization_url failed for %s", provider)
            return self.error(message="Failed to start calendar connect.", status_code=400)
        return self.success(
            data={"authorization_url": auth_url},
            message="Authorization URL generated",
        )

    @extend_schema(exclude=True)
    def callback_slashless(self, request):
        return self.callback(request)

    @action(detail=False, methods=["get"])
    def callback(self, request):
        """OAuth redirect target: exchange the code and store the integration.

        The provider is carried in the signed `state`, so one callback URL serves any
        provider we add later."""
        from core.services import calendars

        if not settings.SCHEDULING_TOKEN_ENCRYPTION_KEY:
            return self.error(message="Missing SCHEDULING_TOKEN_ENCRYPTION_KEY config.", status_code=500)

        code = request.query_params.get("code")
        state = request.query_params.get("state")
        if not code or not state:
            return self.error(message="Missing code or state.", status_code=400)
        try:
            payload = signing.loads(state, salt=_STATE_SALT, max_age=_STATE_MAX_AGE)
        except signing.BadSignature:
            return self.error(message="Invalid or expired state.", status_code=400)

        nonce = payload.get("nonce")
        if not nonce or not cache.delete(f"oauth_state_{nonce}"):
            return self.error(message="State token already used or invalid.", status_code=400)

        requested = payload.get("provider")
        provider = self._resolve_provider(requested)
        if provider is None:
            return self.error(
                message=self._unsupported_provider_message(requested), status_code=400
            )

        tenant = Tenant.objects.filter(pk=payload["tenant_id"]).first()
        if tenant is None:
            return self.error(message="Unknown tenant.", status_code=400)

        try:
            tokens = calendars.get_provider_class(provider).exchange_code(code, state=state)
        except Exception:  # noqa: BLE001
            logger.exception("%s code exchange failed", provider)
            return self.error(message="Failed to connect calendar.", status_code=400)

        # Look up on the same key as the unique constraint (tenant, user) — NOT on
        # provider. Filtering by provider would miss a row belonging to a different
        # one, build a second row, and hit the constraint as an uncaught IntegrityError
        # with the auth code already spent.
        integration = CalendarIntegration.objects.filter(
            tenant=tenant,
            user_id=payload["user_id"],
            is_deleted=False,
        ).first()
        if integration is None:
            integration = CalendarIntegration(
                tenant=tenant,
                user_id=payload["user_id"],
            )
        integration.provider = provider
        integration.account_email = tokens.account_email
        integration.scope = tokens.scope
        integration.token_expires_at = tokens.token_expires_at
        integration.sync_status = CalendarIntegration.SyncStatus.CONNECTED
        integration.last_error = ""
        integration.set_tokens(
            access_token=tokens.access_token, refresh_token=tokens.refresh_token
        )
        integration.save()

        # Establish the push-notification watch + seed the cache (no-op if webhooks
        # are not configured).
        from core.tasks import establish_calendar_watch

        establish_calendar_watch.delay(str(integration.id))

        frontend = getattr(settings, "FRONTEND_PROJECT_URL", "").rstrip("/")
        if frontend:
            return redirect(f"{frontend}/integrations")
        return self.success(message="Calendar connected successfully.")

    def perform_destroy(self, instance):
        # Disconnect: stop the watch, best-effort revoke, then soft-delete.
        try:
            from core.services import calendar_sync

            calendar_sync.stop_watch(instance)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Calendar stop_watch failed for %s: %s", instance.id, exc)
        try:
            from core.services import calendars

            calendars.get_provider_for(instance).revoke(instance)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Calendar revoke failed for %s: %s", instance.id, exc)
        instance.sync_status = CalendarIntegration.SyncStatus.DISCONNECTED
        instance.save(update_fields=["sync_status", "updated_at"])
        instance.soft_delete(user=self.request.user)
