from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone

from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from core.models.device_token import DeviceToken
from core.serializers.device_token import (
    DeviceTokenDropdownSerializer,
    DeviceTokenListSerializer,
    DeviceTokenSerializer,
)

from drf_spectacular.utils import extend_schema

@extend_schema(tags=["Device Token"])
class DeviceTokenViewSet(TenantBaseViewSet):
    """
    Register/deregister a device's FCM token for push notifications. Users only
    see and manage their own tokens. Registration is an upsert (re-registering a
    token reactivates it).
    """

    queryset = DeviceToken.objects.all()
    serializer_class = DeviceTokenSerializer
    list_serializer_class = DeviceTokenListSerializer
    dropdown_serializer_class = DeviceTokenDropdownSerializer
    http_method_names = ["get", "post", "delete", "head", "options"]
    filterset_fields = ["platform", "is_active"]
    ordering = ["-created_at"]

    def _tenant(self, request):
        try:
            return request.user.tenant_mapping.tenant
        except Exception:
            raise PermissionDenied(
                "Your user account is not associated with any active tenant."
            )

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.request.user.is_superuser:
            return queryset
        return queryset.filter(user=self.request.user)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tenant = self._tenant(request)
        token = serializer.validated_data["token"]
        platform = serializer.validated_data.get("platform", DeviceToken.Platform.WEB)
        now = timezone.now()

        device = DeviceToken.objects.filter(
            tenant=tenant, token=token, is_deleted=False
        ).first()

        if device is None:
            # Read-then-write against the partial unique index on (tenant, token).
            # Clients are told to POST on every app start, so a launch that fires the
            # call twice races here and the loser used to surface as a 500. Catching
            # the constraint and re-reading turns that into the update branch, which
            # is what the second request wanted anyway.
            #
            # atomic() is required, not decorative: without the savepoint the
            # IntegrityError leaves the surrounding transaction unusable and the
            # recovery query fails too.
            try:
                with transaction.atomic():
                    device = DeviceToken.objects.create(
                        tenant=tenant,
                        user=request.user,
                        token=token,
                        platform=platform,
                        last_seen_at=now,
                    )
                    created = True
            except IntegrityError:
                device = DeviceToken.objects.filter(
                    tenant=tenant, token=token, is_deleted=False
                ).first()
                if device is None:
                    # The constraint fired for a reason we have not accounted for.
                    raise
                created = False
        else:
            created = False

        # Tracked explicitly rather than inferred from the row's state — comparing
        # last_seen_at against `now` would skip the refresh on a timestamp collision
        # and silently leave user/platform/is_active stale.
        if not created:
            device.user = request.user
            device.platform = platform
            device.is_active = True
            device.last_seen_at = now
            device.save(
                update_fields=["user", "platform", "is_active", "last_seen_at", "updated_at"]
            )

        return self.success(
            data=DeviceTokenSerializer(device).data,
            message="Device registered",
            status_code=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="deactivate")
    def deactivate(self, request):
        token = request.data.get("token")
        if not token:
            return self.error(message="token is required", status_code=400)
        tenant = self._tenant(request)
        DeviceToken.objects.filter(
            tenant=tenant, user=request.user, token=token
        ).update(is_active=False)
        return self.success(message="Device deactivated")
