from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.throttling import ScopedRateThrottle
from django.db import IntegrityError, transaction
from django.utils import timezone

from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from core.models.device_token import DeviceToken
from core.serializers.device_token import (
    DeviceTokenDropdownSerializer,
    DeviceTokenListSerializer,
    DeviceTokenSerializer,
    DeviceTokenTestRequestSerializer,
    DeviceTokenTestResponseSerializer,
)
from core.services import device_tokens as token_service
from core.services import push as push_service

from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view

@extend_schema(tags=["Device Token"])
@extend_schema_view(
    create=extend_schema(
        responses={
            201: DeviceTokenSerializer,
            429: OpenApiResponse(
                description=(
                    "More than DEVICE_TOKEN_REGISTER_RATE registrations from this user "
                    "in the window. Clients are expected to POST on every app start, so "
                    "this only fires for a looping client — back off and retry after the "
                    "interval in `Retry-After`."
                )
            ),
        }
    )
)
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
    #: Per-action throttle scopes. `ScopedRateThrottle` reads a single
    #: `throttle_scope` attribute, so the scope is selected in get_throttles()
    #: rather than declared once on the class.
    #:
    #: `test` is deliberately absent: the diagnostic send is NOT rate-limited for
    #: now, because a limit gets in the way while push is still being set up — the
    #: exact moment someone needs to press it repeatedly. To restore it, add
    #: `"test": "device_token_test"` here and a matching entry to
    #: REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"].
    THROTTLE_SCOPES = {
        "create": "device_token_register",
    }
    throttle_scope = "device_token_register"

    def get_throttles(self):
        """Rate-limit registration only.

        Clients are told to POST on every app start, so this is the one write whose
        legitimate volume already looks like abuse — and it accepts any 512-char
        string, so a looping client can insert unbounded junk.

        Read actions are left alone: one scope viewset-wide would cap a device list
        at the registration rate.
        """
        scope = self.THROTTLE_SCOPES.get(self.action)
        if scope:
            self.throttle_scope = scope
            return [ScopedRateThrottle()]
        return super().get_throttles()

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

        # Which login this device is registering under. Resolved here rather than at
        # login because the token does not exist yet at login time — the client has to
        # ask Firebase for it first, which is a separate round trip.
        session = token_service.current_session(request)

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
                        session=session,
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
            # Registration is the self-heal path, so it clears the send-side verdict
            # as well as the flag. Leaving a stale strike count would let a token
            # that had two bad sends months ago be retired by a single unrelated
            # failure after the client came back healthy.
            device.failure_count = 0
            device.last_error = ""
            # The link moves to the newest login, so revoking an older session cannot
            # silence a device that has since been re-registered under a live one.
            # Only overwritten when we could resolve a session — a re-registration that
            # arrives without one must not blank a good link.
            if session is not None:
                device.session = session
            device.save(
                update_fields=[
                    "user",
                    "platform",
                    "is_active",
                    "last_seen_at",
                    "failure_count",
                    "last_error",
                    "session",
                    "updated_at",
                ]
            )

        # After the write, so the device just registered is never the one retired.
        token_service.enforce_user_cap(tenant, request.user)

        return self.success(
            data=DeviceTokenSerializer(device).data,
            message="Device registered",
            status_code=status.HTTP_201_CREATED,
        )

    @extend_schema(
        request=DeviceTokenTestRequestSerializer,
        responses={
            200: DeviceTokenTestResponseSerializer,
            400: OpenApiResponse(
                description=(
                    "You have no device we would deliver to — either none registered, "
                    "or every one is inactive or has not been re-registered inside "
                    "DEVICE_TOKEN_STALE_DAYS. `data.registered` says which."
                )
            ),
            503: OpenApiResponse(
                description=(
                    "Push is not configured on this server (`FIREBASE_CREDENTIALS` is "
                    "unset), so nothing could be sent by anyone."
                )
            ),
        },
    )
    @action(detail=False, methods=["post"], url_path="test")
    def test(self, request):
        """Send a test push to your own devices and report what happened.

        Exists because every other signal is ambiguous. Registration returning `201`
        proves the token was *stored*, never that a notification will arrive: push
        may be switched off server-side, the token may have aged out of the
        deliverable window, or FCM may have disowned the device since. This is the
        only call that answers "will I actually receive notifications?".

        The failure modes are separated deliberately rather than collapsed into one
        "0 sent" response:

        * **503** — push is off server-side. Nothing the caller can fix.
        * **400** — the caller has no deliverable device. Fixable by registering.
        * **200** — an attempt was made; `results` says what each device did.

        A `200` with `accepted > 0` still is not proof of delivery. FCM accepting a
        message means it has taken it, and the send API has no per-device receipt.
        """
        tenant = self._tenant(request)
        serializer = DeviceTokenTestRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = (serializer.validated_data.get("token") or "").strip()

        report = push_service.send_test_to_user(
            tenant, request.user, token=token or None
        )

        if not report["push_enabled"]:
            return self.error(
                message=(
                    "Push notifications are not configured on this server, so no "
                    "test could be sent."
                ),
                errors=report,
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        if report["configuration_error"]:
            # "Set but unusable" is its own state. Reporting it as "not configured"
            # sends an operator looking for a missing variable that is right there.
            return self.error(
                message=(
                    "Push is configured but the Firebase credentials could not be "
                    f"loaded: {report['configuration_error']}"
                ),
                errors=report,
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        if not report["targeted"]:
            hint = (
                "You have no registered device. Register one and try again."
                if not report["registered"]
                else (
                    f"You have {report['registered']} registered device(s), but none "
                    "are active and recently re-registered. Open the app on the "
                    "device to re-register it."
                )
            )
            return self.error(
                message=hint,
                errors=report,
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        return self.success(
            data=report,
            message=(
                f"Test notification accepted by FCM for {report['accepted']} of "
                f"{report['targeted']} device(s)."
            ),
            status_code=status.HTTP_200_OK,
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
