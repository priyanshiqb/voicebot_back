import logging

from drf_spectacular.utils import extend_schema
from rest_framework import status

from tenant.common.views.tenant_base_api_view import TenantBaseAPIView
from tenant.models.TenantSettings import TenantSettings
from tenant.serializers.tenant_settings import TenantSettingsSerializer

logger = logging.getLogger(__name__)


@extend_schema(tags=["Tenant Settings"])
class TenantSettingsView(TenantBaseAPIView):
    """
    Per-tenant configuration, read and written as a singleton — there is one row
    per tenant, so there is no id in the path and no list.

    Currently holds one setting: which SMTP configuration carries booking email.

    > Booking email has **no platform fallback**. Leave
    > `booking_email_configuration` unset and booking confirmations, reminders,
    > cancellations and reschedule requests are **not sent**.
    """

    def _settings(self):
        tenant = self.get_tenant()
        if tenant is None:
            # A superuser with no tenant mapping. There is no "all tenants'
            # settings" answer to give, so say so rather than inventing a row.
            return None
        return TenantSettings.for_tenant(tenant.id)

    @extend_schema(responses={200: TenantSettingsSerializer})
    def get(self, request):
        settings_row = self._settings()
        if settings_row is None:
            return self.error(
                message="No tenant context. Superusers must act within a tenant.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        serializer = TenantSettingsSerializer(
            settings_row, context={"request": request}
        )
        return self.success(data=serializer.data, message="Retrieved successfully")

    @extend_schema(
        request=TenantSettingsSerializer, responses={200: TenantSettingsSerializer}
    )
    def patch(self, request):
        settings_row = self._settings()
        if settings_row is None:
            return self.error(
                message="No tenant context. Superusers must act within a tenant.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        serializer = TenantSettingsSerializer(
            settings_row,
            data=request.data,
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=request.user)

        # Logged because it changes whether a tenant's customers receive booking
        # email at all, and "our confirmations stopped arriving" is otherwise a
        # support ticket with nothing to look at.
        logger.info(
            "tenant %s booking email configuration set to %s",
            settings_row.tenant_id,
            serializer.instance.booking_email_configuration_id,
            extra={
                "tenant_id": str(settings_row.tenant_id),
                "configuration_id": str(
                    serializer.instance.booking_email_configuration_id or ""
                ),
                "actor_id": str(request.user.id),
            },
        )
        return self.success(data=serializer.data, message="Updated successfully")
