from django.contrib import admin
from onboard.models.billing_address import BillingAddress
from utils.admin.base_admin_core import BaseAdminCore


@admin.register(BillingAddress)
class BillingAddressAdmin(BaseAdminCore):
    """
    BillingAddress ModelAdmin interface configuration.
    Registers BillingAddress model inheriting from BaseAdminCore.
    """
    model = BillingAddress

    list_display = (
        "id",
        "temp_user",
        "user",
        "line1",
        "city",
        "state",
        "country",
        "gstin",
        "is_active",
        "created_at",
    )

    list_filter = (
        "country",
        "state",
    ) + BaseAdminCore.list_filter

    search_fields = (
        "line1",
        "city",
        "state",
        "zip_code",
        "gstin",
    ) + BaseAdminCore.search_fields

    fieldsets = (
        ("Associations", {
            "fields": (
                "id",
                "temp_user",
                "user",
            )
        }),
        ("Address Details", {
            "fields": (
                "line1",
                "city",
                "state",
                "zip_code",
                "country",
            )
        }),
        ("Taxation info", {
            "fields": (
                "gstin",
            )
        }),
        ("Timestamps", {
            "fields": (
                "created_at",
                "updated_at",
                "deleted_at",
            )
        }),
    )
