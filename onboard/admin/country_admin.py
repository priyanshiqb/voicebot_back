from django.contrib import admin
from onboard.models import Country
from utils.admin.base_admin_core import BaseAdminCore

@admin.register(Country)
class CountryAdmin(BaseAdminCore):
    list_display = ("name", "code", "dial_code", "format_mask", "id")
    search_fields = ("id", "name", "code", "dial_code")
    readonly_fields = ("id",)
    list_filter = ()
    ordering = ("name",)
    actions = ()

    fieldsets = (
        ("Geography Information", {
            "fields": ("id", "name", "code")
        }),
        ("Telephony Information", {
            "fields": ("dial_code", "format_mask")
        }),
    )
