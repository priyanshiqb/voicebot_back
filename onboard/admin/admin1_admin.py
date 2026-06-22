from django.contrib import admin
from onboard.models import Admin1
from utils.admin.base_admin_core import BaseAdminCore

@admin.register(Admin1)
class Admin1Admin(BaseAdminCore):
    list_display = ("name", "code", "country", "id")
    search_fields = ("id", "name", "code", "country__name", "country__code")
    readonly_fields = ("id",)
    list_filter = ("country",)
    ordering = ("name",)
    actions = ()
    autocomplete_fields = ("country",)

    fieldsets = (
        ("Geography Information", {
            "fields": ("id", "name", "code", "country")
        }),
    )
