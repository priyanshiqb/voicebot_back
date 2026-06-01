from django.contrib import admin
from onboard.models.subscription import Subscription
from utils.admin.base_admin_core import BaseAdminCore


@admin.register(Subscription)
class SubscriptionAdmin(BaseAdminCore):
    """
    Subscription ModelAdmin interface configuration.
    Registers Subscription model inheriting from BaseAdminCore.
    """
    model = Subscription

    list_display = (
        "id",
        "tenant",
        "plan",
        "status",
        "start_date",
        "end_date",
        "is_active",
        "created_at",
    )

    list_filter = (
        "status",
        "plan",
    ) + BaseAdminCore.list_filter

    search_fields = (
        "tenant__name",
        "tenant__subdomain",
        "plan__name",
        "plan__code",
    ) + BaseAdminCore.search_fields

    readonly_fields = BaseAdminCore.readonly_fields + (
        "plan_snapshot",
    )

    fieldsets = (
        ("Subscription Details", {
            "fields": (
                "id",
                "tenant",
                "plan",
                "status",
            )
        }),
        ("Validity Period", {
            "fields": (
                "start_date",
                "end_date",
            )
        }),
        ("Plan Snapshot Details", {
            "fields": (
                "plan_snapshot",
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
