"""The operator audit log, strictly read-only.

Engineering's window onto what platform staff did. Read-only for the same reason the
credit ledger is: an audit trail that can be edited from the admin is not one, and the
model would refuse the write anyway — leaving the buttons on only offers staff
something that errors.
"""

from django.contrib import admin

from user.models import OperatorAuditLog
from utils.admin.base_admin_core import BaseAdminCore


@admin.register(OperatorAuditLog)
class OperatorAuditLogAdmin(BaseAdminCore):
    model = OperatorAuditLog
    actions = None

    list_display = (
        "created_at",
        "actor",
        "action",
        "tenant",
        "target",
        "outcome",
        "short_reason",
    )
    list_filter = ("action", "outcome", "created_at")
    search_fields = (
        "actor__email",
        "tenant__name",
        "tenant__subdomain",
        "target",
        "reason",
    )
    list_select_related = ("actor", "tenant")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    fieldsets = (
        ("What happened", {
            "fields": ("id", "created_at", "actor", "action", "outcome"),
        }),
        ("To whom", {
            "fields": ("tenant", "target"),
            "description": (
                "`tenant` is null for actions with no single tenant, such as editing "
                "the plan catalogue. `target` names the specific thing acted on."
            ),
        }),
        ("Why, and with what result", {
            "fields": ("reason", "payload", "result"),
            "description": (
                "`reason` is required on every action. A credit grant with no "
                "explanation is indistinguishable from a bug six weeks later."
            ),
        }),
        ("Where from", {
            "fields": ("ip_address", "user_agent"),
            "description": (
                "Evidence, not proof. X-Forwarded-For is client-settable and only "
                "means anything when a proxy you control appends to it."
            ),
        }),
    )

    @admin.display(description="Reason")
    def short_reason(self, obj):
        if len(obj.reason) <= 60:
            return obj.reason
        return obj.reason[:57] + "…"

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
