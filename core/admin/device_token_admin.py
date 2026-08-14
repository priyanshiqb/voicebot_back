from django.contrib import admin, messages

from tenant.common.admin.tenant_base_admin_core import TenantBaseAdminCore
from core.models.device_token import DeviceToken
from core.services import push as push_service


@admin.register(DeviceToken)
class DeviceTokenAdmin(TenantBaseAdminCore):
    """Device tokens, plus the two actions that make push debuggable from here.

    The API's ``/device-tokens/test/`` only ever targets the caller's own devices,
    which is right for a user-facing button and useless when the question is "why
    is *that* person's phone silent". These actions run against whichever rows are
    selected — still tenant-scoped, because ``TenantBaseAdminCore.get_queryset``
    filters the queryset the action receives.
    """

    model = DeviceToken

    list_display = (
        "id",
        "user",
        "tenant",
        "platform",
        "is_active",
        "short_token",
        "last_error",
        "failure_count",
        "last_seen_at",
        "created_at",
    )
    list_filter = ("platform", "is_active") + TenantBaseAdminCore.list_filter
    search_fields = ("token", "user__email")
    ordering = ("-created_at",)
    actions = ("send_test_notification", "validate_with_fcm")

    @admin.display(description="Token")
    def short_token(self, obj):
        """Enough of the token to identify a row without making the list unreadable."""
        if not obj.token:
            return "—"
        return f"{obj.token[:18]}…{obj.token[-6:]}" if len(obj.token) > 30 else obj.token

    def _push_unavailable(self, request):
        """Report why nothing can be sent, or None if push is usable.

        Checked once per action rather than per row: "not configured" is a property
        of the server, and repeating it for every selected device would bury the
        per-device results the operator actually came for.
        """
        if not push_service.is_configured():
            return (
                "Push is not configured on this server (FIREBASE_CREDENTIALS is "
                "unset), so nothing was sent."
            )
        error = push_service.configuration_error()
        if error:
            return f"Firebase credentials are set but could not be loaded: {error}"
        return None

    @admin.action(description="Send a test notification to the selected device(s)")
    def send_test_notification(self, request, queryset):
        """Deliver the same diagnostic push the API endpoint sends.

        Reports each device separately. A row that FCM rejects is retired by the
        send path exactly as it would be during a real notification — this is a real
        send, not a simulation, which is the point.
        """
        unavailable = self._push_unavailable(request)
        if unavailable:
            self.message_user(request, unavailable, level=messages.ERROR)
            return

        devices = list(queryset)
        tokens = [device.token for device in devices if device.token]
        if not tokens:
            self.message_user(
                request, "None of the selected rows has a token.", level=messages.WARNING
            )
            return

        accepted, outcomes = push_service.send_test_to_tokens(tokens)

        for device in devices:
            outcome = outcomes.get(device.token) or {}
            label = f"{device.user} · {device.platform} · {self.short_token(device)}"
            if outcome.get("accepted"):
                self.message_user(
                    request,
                    f"Accepted by FCM: {label}. Acceptance is not delivery — if "
                    "nothing appears, the cause is on the device.",
                    level=messages.SUCCESS,
                )
            else:
                device.refresh_from_db(fields=["is_active"])
                suffix = " (device deactivated)" if not device.is_active else ""
                self.message_user(
                    request,
                    f"Not accepted: {label} — {outcome.get('detail') or 'unknown'}{suffix}",
                    level=messages.ERROR,
                )

        skipped = len(devices) - len(tokens)
        if skipped:
            self.message_user(
                request, f"{skipped} row(s) had no token and were skipped.",
                level=messages.WARNING,
            )

    @admin.action(description="Validate the selected token(s) with FCM (sends nothing)")
    def validate_with_fcm(self, request, queryset):
        """Ask FCM whether each token is real, without delivering anything.

        The counterpart to the action above, and the one to reach for when a test
        push is *accepted* but never arrives: it separates "FCM does not know this
        token" from "FCM knows it and the problem is on the device" — which the
        send path alone cannot tell you, because a send to a dead token can still
        be accepted for later delivery.
        """
        unavailable = self._push_unavailable(request)
        if unavailable:
            self.message_user(request, unavailable, level=messages.ERROR)
            return

        for device in queryset:
            label = f"{device.user} · {device.platform} · {self.short_token(device)}"
            verdict = push_service.probe_token(device.token)
            if verdict is True:
                self.message_user(
                    request,
                    f"Alive — FCM recognises this token: {label}",
                    level=messages.SUCCESS,
                )
            elif verdict is False:
                self.message_user(
                    request,
                    f"Dead — FCM rejected this token: {label}. Re-register on the device.",
                    level=messages.ERROR,
                )
            else:
                self.message_user(
                    request,
                    f"Inconclusive (transient failure, not evidence either way): {label}",
                    level=messages.WARNING,
                )
