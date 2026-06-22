from django.contrib import admin
from onboard.models import Admin3
from utils.admin.base_admin_core import BaseAdminCore

@admin.register(Admin3)
class Admin3Admin(BaseAdminCore):
    list_display = ("name", "code", "admin2", "id")
    search_fields = ("id", "name", "code", "admin2__name")
    readonly_fields = ("id",)
    list_filter = ()
    ordering = ("name",)
    actions = ()
    autocomplete_fields = ("admin2",)

    fieldsets = (
        ("Geography Information", {
            "fields": ("id", "name", "code", "admin2")
        }),
    )
