import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema
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

    # Removing the assigned booking sender asks for confirmation rather than
    # refusing outright: the first attempt returns 409, and repeating it with
    # `force=true` goes through, clearing the assignment on the way
    # (`release_from_settings`). Booking email then stops — there is no platform
    # fallback to inherit it.
    #
    # A hard refusal was worse in both directions. It never prevented the outcome —
    # a tenant who wants a configuration gone edits settings and comes back — while
    # making the leaked-credential case slower. But allowing it silently is worse
    # still: switching off one connection among several stops every booking
    # confirmation, and nothing about the request says so. 409-then-force is the
    # only shape that puts the consequence in front of the person deciding.
    # `message` is popup copy — the frontend renders it verbatim in the confirm
    # dialog, so it carries no endpoint paths, no field names and no mention of
    # `force`. How to proceed is a machine concern and lives in
    # `errors.requires_force`; a tenant reading "repeat the request with force=true"
    # learns nothing they can act on.
    CONFIRM_DISCONNECT = "Are you sure you want to disconnect this email integration?"
    CONFIRM_DELETE = "Are you sure you want to delete this email integration?"
    CONFIRM_BULK_DELETE = (
        "Are you sure you want to delete the selected email integrations?"
    )

    # Appended when the integration carries demo booking mail. States the
    # consequence rather than the mechanism: what stops, and what makes it start
    # again.
    #
    # Two variants because the subject differs. Only one configuration per tenant
    # can be the booking sender, so a bulk selection contains at most one — "It is
    # currently used" after "the selected integrations?" reads as though all of
    # them are.
    BOOKING_SENDER_NOTE = (
        "It is currently used for demo booking emails, so those emails will stop "
        "until you connect another one."
    )
    BOOKING_SENDER_NOTE_BULK = (
        "One of them is currently used for demo booking emails, so those emails "
        "will stop until you connect another one."
    )

    #: Accepted spellings of "yes". `force=1` is what a query string usually
    #: carries, `true` what JSON does, and a client that sends the wrong one and is
    #: silently ignored would look like the confirmation simply does not work.
    _TRUTHY = frozenset({"true", "1", "yes", "on"})

    def _tenant_id(self):
        mapping = getattr(self.request.user, "tenant_mapping", None)
        return getattr(mapping, "tenant_id", None)

    def _force_requested(self, request):
        """Whether the caller confirmed, from `?force=` or a `force` body field.

        Both, because the three endpoints this guards do not share a shape: DELETE
        carries no body in most HTTP clients, while `disconnect/` and `bulk_delete`
        are POSTs where a query string would be the odd one out.
        """
        raw = request.query_params.get("force")
        if raw is None and isinstance(request.data, dict):
            raw = request.data.get("force")
        if raw is None:
            return False
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in self._TRUTHY

    def _needs_confirmation(self, request, prompt, is_booking_sender, note=None):
        """A 409 carrying confirm-dialog copy, or None to proceed.

        The message is assembled as a generic prompt plus whatever consequences
        apply — today only the booking-sender one. Composing it here rather than
        writing three fixed strings means a second consequence added later appears
        in every dialog at once instead of in whichever call site someone
        remembered.

        409 rather than 400: the request is well formed and the caller is
        permitted; what is missing is a decision. A frontend can branch on the
        status alone to raise the dialog, with no risk of confusing this with a
        validation failure.
        """
        notes = []
        if is_booking_sender:
            notes.append(note or self.BOOKING_SENDER_NOTE)

        if not notes or self._force_requested(request):
            return None

        return self.error(
            message=" ".join([prompt, *notes]),
            errors={"code": "BOOKING_SENDER_IN_USE", "requires_force": True},
            status_code=status.HTTP_409_CONFLICT,
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
            # Redundant with the model's soft_delete, which releases too — kept
            # because this method is the documented delete path and a settings row
            # pointing at a deleted configuration reads as configured when it is
            # not. Both are idempotent.
            instance.release_from_settings()
            instance.soft_delete(user=self.request.user)
            TenantEmailConfiguration.ensure_default(tenant_id)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        needs_confirmation = self._needs_confirmation(
            request, self.CONFIRM_DELETE, instance.is_booking_sender()
        )
        if needs_confirmation is not None:
            return needs_confirmation

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

    @extend_schema(
        request={"application/json": {"example": {"force": True}}},
        responses={
            200: TenantEmailConfigurationSerializer,
            409: OpenApiResponse(
                description=(
                    "This configuration is the assigned booking sender. Confirm by "
                    "repeating the request with force=true; booking email will stop."
                )
            ),
        },
    )
    @action(detail=True, methods=["post"])
    def disconnect(self, request, pk=None):
        """Stop using this configuration, keeping its credentials for later.

        Deliberately not the same as deleting it: a tenant pausing a provider
        should not have to re-enter an API key to come back. If this was the
        default, a connected replacement is promoted — disconnecting must never
        drop a tenant onto the platform sender while a working alternative sits
        unused.

        If this was the assigned booking sender, the assignment is cleared and
        **booking email stops**. Allowed on purpose: refusing did not prevent that
        outcome, it only made the tenant edit settings first.
        """
        config = self.get_object()
        needs_confirmation = self._needs_confirmation(
            request, self.CONFIRM_DISCONNECT, config.is_booking_sender()
        )
        if needs_confirmation is not None:
            return needs_confirmation

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
        request={
            "application/json": {
                "example": {"ids": ["uuid-1", "uuid-2"], "force": False}
            }
        },
        responses={
            200: None,
            409: OpenApiResponse(
                description=(
                    "One of the ids is the assigned booking sender. Confirm by "
                    "repeating the request with force=true; booking email will stop."
                )
            ),
        },
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
            assigned = self.get_queryset().filter(
                id__in=ids, booking_settings__isnull=False
            )
            needs_confirmation = self._needs_confirmation(
                request,
                self.CONFIRM_BULK_DELETE,
                assigned.exists(),
                note=self.BOOKING_SENDER_NOTE_BULK,
            )
            if needs_confirmation is not None:
                return needs_confirmation

        tenant_id = self._tenant_id()
        response = super().bulk_delete(request)
        if response.status_code == status.HTTP_200_OK and tenant_id:
            TenantEmailConfiguration.ensure_default(tenant_id)
        return response
