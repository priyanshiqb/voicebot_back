from django.contrib import admin
from onboard.models.temp_user import TempUser
from utils.admin.base_admin_core import BaseAdminCore


@admin.register(TempUser)
class TempUserAdmin(BaseAdminCore):
    """
    TempUser ModelAdmin interface configuration.
    Registers TempUser model inheriting from BaseAdminCore.
    """
    model = TempUser

    list_display = (
        "id",
        "email",
        "phone_number",
        "first_name",
        "last_name",
        "business_name",
        "selected_plan",
        "is_email_verified",
        "is_active",
        "created_at",
    )

    list_filter = (
        "is_email_verified",
        "selected_plan",
    ) + BaseAdminCore.list_filter

    search_fields = (
        "email",
        "phone_number",
        "first_name",
        "last_name",
        "business_name",
        "referral_code",
    ) + BaseAdminCore.search_fields

    fieldsets = (
        ("Personal Details", {
            "fields": (
                "id",
                "email",
                "phone_number",
                "first_name",
                "last_name",
                "business_name",
            )
        }),
        ("Authentication & Security", {
            "fields": (
                "password_hash",
                "otp_code",
                "otp_created_at",
                "is_email_verified",
            )
        }),
        ("Plan & Referrals", {
            "fields": (
                "selected_plan",
                "referral_code",
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
