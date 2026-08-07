from django.contrib import admin

from tenant.common import TenantBaseAdmin
from tenant.models.TenantSettings import TenantSettings


@admin.register(TenantSettings)
class TenantSettingsAdmin(TenantBaseAdmin):
    """Per-tenant configuration.

    `booking_email_sends` is the column worth scanning: a populated
    `booking_email_configuration` does not mean booking mail is going out — the
    configuration behind it can be disconnected, or reset to UNVERIFIED by a
    credential edit long after the pointer was set.
    """

    list_display = ("tenant", "booking_email_configuration", "booking_email_sends")
    list_filter = ("is_active", "is_deleted")
    search_fields = ("tenant__name", "id")
    autocomplete_fields = ()
    raw_id_fields = ("booking_email_configuration",)

    @admin.display(boolean=True, description="Booking email sending")
    def booking_email_sends(self, obj):
        return TenantSettings.booking_sender(obj.tenant_id) is not None
