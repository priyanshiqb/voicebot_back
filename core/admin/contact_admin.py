from django.contrib import admin

from core.models.contact import Contact
from tenant.common.admin.tenant_base_admin import TenantBaseAdmin


@admin.register(Contact)
class ContactAdmin(TenantBaseAdmin):
    model = Contact

    list_display = (
        "id",
        "full_name",
        "phone_e164",
        "email_address",
        "company",
        "consent_status",
        "do_not_call",
        "source",
        "is_active",
        "created_at",
    )
    list_filter = (
        "consent_status",
        "do_not_call",
        "source",
    ) + TenantBaseAdmin.list_filter
    search_fields = (
        "first_name",
        "last_name",
        "phone_e164",
        "phone_number",
        "email_address",
        "company",
    )
    ordering = ("-created_at",)

    # Derived on save — editing them here would let the identity key drift from the
    # raw number the rest of the system displays.
    readonly_fields = TenantBaseAdmin.readonly_fields + ("phone_e164", "phone_region")
