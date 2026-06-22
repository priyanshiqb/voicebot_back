from django.contrib import admin
from onboard.models import Country
from utils.admin.base_admin_core import BaseAdminCore

@admin.register(Country)
class CountryAdmin(BaseAdminCore):
    list_display = ("name", "code", "id")
    search_fields = ("id", "name", "code")
    readonly_fields = ("id",)
    list_filter = ()
    ordering = ("name",)
    actions = ()

    fieldsets = (
        ("Geography Information", {
            "fields": ("id", "name", "code")
        }),
    )
