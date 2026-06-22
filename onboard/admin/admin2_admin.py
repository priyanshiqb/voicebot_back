from django.contrib import admin
from onboard.models import Admin2
from utils.admin.base_admin_core import BaseAdminCore

@admin.register(Admin2)
class Admin2Admin(BaseAdminCore):
    list_display = ("name", "code", "admin1", "id")
    search_fields = ("id", "name", "code", "admin1__name", "admin1__country__name")
    readonly_fields = ("id",)
    list_filter = ()
    ordering = ("name",)
    actions = ()
    autocomplete_fields = ("admin1",)

    fieldsets = (
        ("Geography Information", {
            "fields": ("id", "name", "code", "admin1")
        }),
    )
