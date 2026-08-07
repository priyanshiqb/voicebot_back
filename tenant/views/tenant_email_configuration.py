import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.throttling import UserRateThrottle

from tenant.common import TenantBaseViewSet
from tenant.models.TenantEmailConfiguration import TenantEmailConfiguration
from tenant.serializers.tenant_email_configuration import (
    TenantEmailConfigurationDropdownSerializer,
    TenantEmailConfigurationListSerializer,
    TenantEmailConfigurationSerializer,
)
from utils.mail import connection as mail_connection
from utils.mail import providers as mail_providers

logger = logging.getLogger(__name__)


class SMTPTestRateThrottle(UserRateThrottle):
    """An authenticated endpoint that sends mail is an open relay with extra
    steps. The recipient is pinned to the caller's own address, which removes the
    abuse case, and this bounds the noise.

    **Per user, not per tenant.** ``UserRateThrottle`` keys the bucket on the user
    id, so a tenant's effective budget is this rate multiplied by its user count,
    and one user testing three different configurations shares a single bucket.

    The rate is read from ``settings.SMTP_TEST_THROTTLE_PER_HOUR`` at request time
    — DRF instantiates a throttle per request, so no caching to work around. It
    still falls back to 5 if the setting is missing entirely: this endpoint must be
    throttled the moment it exists, and a deployment that loses a settings line
    should get the safe rate rather than an unthrottled relay.
    """

    scope = "smtp_test"

    DEFAULT_PER_HOUR = 5

    def get_rate(self):
        return f"{getattr(settings, 'SMTP_TEST_THROTTLE_PER_HOUR', self.DEFAULT_PER_HOUR)}/hour"


@extend_schema(tags=["Tenant Email Configuration"])
class TenantEmailConfigurationViewSet(TenantBaseViewSet):
    """
    Manage a tenant's own outbound mail credentials.

    A tenant may connect several configurations — a second provider to fail over
    to, a replacement being tested before cutover — but **exactly one is the
    default**, and the default is what will be used to reach that tenant's
    customers. Both halves of that rule are maintained here: the first
    configuration created becomes the default, and deleting the default promotes
    another.

    Nothing in the product sends through these credentials yet. Saving one is
    currently inert by design; the send path still uses the platform sender.
    """

    queryset = TenantEmailConfiguration.objects.all()
    serializer_class = TenantEmailConfigurationSerializer
    list_serializer_class = TenantEmailConfigurationListSerializer
    dropdown_serializer_class = TenantEmailConfigurationDropdownSerializer

    # No PUT: a full replace of a credential record invites a client to omit
    # `password` and wipe it. PATCH makes the partial intent explicit.
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    filterset_fields = ["provider", "status", "is_default", "is_active"]
    search_fields = ["name", "host", "from_email"]
    ordering_fields = ["created_at", "updated_at", "name", "status"]
    # Default first: it is the one that matters, and a list that buries it under
    # whatever was edited most recently makes the important row hard to find.
    ordering = ["-is_default", "-created_at"]

    # Refusing rather than warning, because booking mail has no platform fallback:
    # taking the assigned sender out of service stops those messages entirely. The
    # tenant has to name a replacement, or say explicitly that booking mail should
    # stop — both of which are one PATCH away, so this is a speed bump, not a trap.
    # A leaked credential can still be dealt with immediately by pointing the
    # setting elsewhere or clearing it.
    BOOKING_SENDER_LOCKED = (
        "This configuration is currently sending your booking email. Assign a "
        "different one at /api/v1/tenant/settings/ first, or clear the setting to "
        "stop sending booking email."
    )

    def _tenant_id(self):
        mapping = getattr(self.request.user, "tenant_mapping", None)
        return getattr(mapping, "tenant_id", None)

    def _booking_sender_block(self, config):
        """A 400 when this configuration cannot be taken out of service, else None."""
        if not config.is_booking_sender():
            return None
        return self.error(
            message=self.BOOKING_SENDER_LOCKED,
            errors={"code": "BOOKING_SENDER_IN_USE"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # --- write paths that maintain the single-default invariant ---------------

    def perform_create(self, serializer, **kwargs):
        """Save, then make this the default if the tenant has none.

        One transaction, so a failure cannot leave a tenant holding a
        configuration with no default — which is silent: mail keeps flowing on the
        platform sender until somebody notices the wrong address.
        """
        with transaction.atomic():
            super().perform_create(serializer, **kwargs)
            config = serializer.instance
            has_default = (
                TenantEmailConfiguration.objects.filter(
                    tenant_id=config.tenant_id, is_default=True
                )
                .exclude(pk=config.pk)
                .exists()
            )
            if not has_default:
                config.make_default()

    def perform_destroy(self, instance):
        tenant_id = instance.tenant_id
        with transaction.atomic():
            # Belt and braces: destroy() refuses an assigned sender before reaching
            # here, but bulk paths and future callers land in this method directly,
            # and a settings row pointing at a deleted configuration reads as
            # configured when it is not.
            instance.release_from_settings()
            instance.soft_delete(user=self.request.user)
            TenantEmailConfiguration.ensure_default(tenant_id)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        blocked = self._booking_sender_block(instance)
        if blocked is not None:
            return blocked
        self.perform_destroy(instance)
        return self.success(message="Deleted successfully")

    @extend_schema(request=None, responses={200: TenantEmailConfigurationSerializer})
    @action(detail=True, methods=["post"], url_path="set-default")
    def set_default(self, request, pk=None):
        """Promote this configuration; the previous default is demoted.

        The only way `is_default` moves. There is deliberately no way to clear it
        without naming a replacement, because a tenant with configurations and no
        default is a tenant whose customer mail silently reverts to our address.
        """
        config = self.get_object()
        config.make_default()
        return self.success(
            data=self.get_serializer(config).data,
            message="Default email configuration updated",
        )

    @extend_schema(request=None, responses={200: TenantEmailConfigurationSerializer})
    @action(detail=True, methods=["post"])
    def disconnect(self, request, pk=None):
        """Stop using this configuration, keeping its credentials for later.

        Deliberately not the same as deleting it: a tenant pausing a provider
        should not have to re-enter an API key to come back. If this was the
        default, a connected replacement is promoted — disconnecting must never
        drop a tenant onto the platform sender while a working alternative sits
        unused.
        """
        config = self.get_object()
        blocked = self._booking_sender_block(config)
        if blocked is not None:
            return blocked
        config.disconnect()
        config.refresh_from_db()
        return self.success(
            data=self.get_serializer(config).data,
            message="Email configuration disconnected",
        )

    @extend_schema(request=None, responses={200: TenantEmailConfigurationSerializer})
    @action(detail=True, methods=["post"])
    def reconnect(self, request, pk=None):
        """Put this configuration back into service.

        Becomes the default if the tenant's current default is disconnected —
        reconnecting your only working sender should not also require remembering
        to promote it.
        """
        config = self.get_object()
        config.reconnect()
        return self.success(
            data=self.get_serializer(config).data,
            message="Email configuration reconnected",
        )

    @extend_schema(request=None, responses={200: None})
    @action(
        detail=True,
        methods=["post"],
        throttle_classes=[SMTPTestRateThrottle],
    )
    def test(self, request, pk=None):
        """Send a verification message through this configuration.

        The recipient is always the caller's own address — never a value from the
        request. An endpoint that mails an arbitrary address on demand is a relay,
        and the person configuring SMTP is the person who needs to see the result.

        A success promotes the configuration to VERIFIED. Testing a non-default
        configuration leaves the current default untouched, which is the point:
        a replacement provider can be proven before cutover rather than after.
        """
        config = self.get_object()
        recipient = (getattr(request.user, "email", "") or "").strip()
        if not recipient:
            return self.error(
                message="Your account has no email address to send the test to.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        ok, error_message = mail_connection.send_test_email(config, recipient)
        now = timezone.now()

        # The configuration's own status/last_tested_at is the current state; this
        # is the history. "They tried five times and it failed every time" is the
        # shape of a support ticket, and a single overwritten field cannot show it.
        from user.models.email_delivery_log import EmailDeliveryLog
        from user.services import email_log

        email_log.mark(
            email_log.record(
                EmailDeliveryLog.Purpose.SMTP_TEST,
                recipient,
                request=request,
                user=request.user,
                tenant_id=config.tenant_id,
            ),
            EmailDeliveryLog.Status.SENT if ok else EmailDeliveryLog.Status.FAILED,
            error="" if ok else error_message,
        )

        config.last_tested_at = now
        if ok:
            config.status = TenantEmailConfiguration.Status.VERIFIED
            config.last_error = ""
            if config.verified_at is None:
                config.verified_at = now
        else:
            config.status = TenantEmailConfiguration.Status.FAILED
            config.last_error = error_message[:2000]
        config.save(
            update_fields=[
                "status",
                "last_tested_at",
                "verified_at",
                "last_error",
                "updated_at",
            ]
        )

        if not ok:
            return self.error(
                message="The test message could not be sent.",
                errors={"detail": error_message},
                data=self.get_serializer(config).data,
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        return self.success(
            data=self.get_serializer(config).data,
            message=f"Test message sent to {recipient}.",
        )

    @extend_schema(responses={200: None})
    @action(detail=False, methods=["get"])
    def providers(self, request):
        """The provider preset catalogue for the setup form.

        Static reference data, identical for every tenant. Served from here so the
        frontend is not a hardcoded copy of a dict that lives in the backend — the
        per-provider `username_hint` in particular is what stops a tenant putting
        their SendGrid login where the literal string `apikey` belongs.
        """
        return self.success(
            data=mail_providers.catalogue(),
            message="Provider presets retrieved successfully",
        )

    @extend_schema(
        request={"application/json": {"example": {"ids": ["uuid-1", "uuid-2"]}}},
        responses={200: None},
    )
    @action(detail=False, methods=["post"])
    def bulk_delete(self, request):
        """Inherited bulk delete, plus the promotion the base implementation knows
        nothing about.

        Without this override, deleting a set that happens to include the default
        leaves the tenant with configurations but no default.

        The decorator has to be repeated: the router discovers extra actions by
        looking for the `mapping` attribute `@action` attaches, so overriding with
        a bare method silently unregisters the route inherited from BaseViewSet.
        """
        ids = request.data.get("ids") or []
        if isinstance(ids, list) and ids:
            assigned = self.get_queryset().filter(id__in=ids, booking_settings__isnull=False)
            if assigned.exists():
                return self.error(
                    message=self.BOOKING_SENDER_LOCKED,
                    errors={"code": "BOOKING_SENDER_IN_USE"},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

        tenant_id = self._tenant_id()
        response = super().bulk_delete(request)
        if response.status_code == status.HTTP_200_OK and tenant_id:
            TenantEmailConfiguration.ensure_default(tenant_id)
        return response
