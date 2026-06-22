from django.contrib import admin
from onboard.models import Place
from utils.admin.base_admin_core import BaseAdminCore

@admin.register(Place)
class PlaceAdmin(BaseAdminCore):
    list_display = ("name", "postal_code", "country", "admin1", "id")
    search_fields = ("id", "name", "postal_code", "country__name", "admin1__name")
    readonly_fields = ("id",)
    list_filter = ("country",)
    ordering = ("name",)
    actions = ()
    autocomplete_fields = ("country", "admin1", "admin2", "admin3")

    fieldsets = (
        ("Location Information", {
            "fields": ("id", "name", "postal_code")
        }),
        ("Hierarchy", {
            "fields": ("country", "admin1", "admin2", "admin3")
        }),
    )
