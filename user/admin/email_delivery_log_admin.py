from django.contrib import admin

from user.models.email_delivery_log import EmailDeliveryLog
from utils.admin.base_admin_core import BaseAdminCore


@admin.register(EmailDeliveryLog)
class EmailDeliveryLogAdmin(BaseAdminCore):
    """
    Read-only view over platform email delivery, for the two questions this table
    was built to answer: *"did the reset mail actually go out?"* and *"who asked
    for it?"*

    **Superuser only, deliberately.** Every other log in this admin is scoped by
    ``TenantBaseAdminCore``, which filters on a ``tenant`` ForeignKey. This model
    has no such key — ``tenant_id`` is a bare UUID precisely so a row outlives the
    tenant it describes — so that scoping cannot be applied here even in
    principle. What is left is a table spanning every tenant plus pre-signup
    ``TempUser`` records, holding email addresses and the IP behind each request.
    Handing that to anyone with ``is_staff`` would leak across tenants far more
    quietly than any API endpoint could.

    **No add, no change.** Rows are written by the system and are evidence; an
    editable audit trail is not one. ``readonly_fields`` restates that field by
    field rather than relying on ``has_change_permission`` alone, so flipping that
    method later does not silently make the evidence writable.

    Deletion stays available to superusers — right-to-erasure requests are real and
    arrive faster than the retention window. It hard-deletes: ``BaseAdminCore``
    hands superusers ``all_objects``, whose ``delete()`` is the ordinary one. That
    is the correct behaviour here and the reason the inherited soft-delete and
    restore actions are dropped below — a soft delete would hide the row while
    leaving the address and the IP in the table, which is the one outcome
    ``user.tasks.purge_email_delivery_logs`` exists to prevent.
    """

    model = EmailDeliveryLog

    list_display = (
        "created_at",
        "purpose",
        "recipient",
        "status",
        "user",
        "sent_at",
        "ip_address",
    )
    # `is_active` / `is_deleted` are dropped from the inherited filters: nothing
    # ever sets them on this model, so they would be two controls that do nothing.
    list_filter = ("purpose", "status", "created_at")
    # Recipient first — a signup or pre-tenant reset has no user row to search by,
    # which is exactly why the address is stored on every row.
    search_fields = ("recipient",)
    date_hierarchy = "created_at"
    list_select_related = ("user",)
    raw_id_fields = ("user",)
    ordering = ("-created_at",)

    fields = (
        "purpose",
        "recipient",
        "status",
        "sent_at",
        "error",
        "user",
        "temp_user_id",
        "tenant_id",
        "ip_address",
        "user_agent",
        "id",
        "created_at",
        "updated_at",
    )
    readonly_fields = fields

    # Empty, not None: Django still contributes the site-wide `delete_selected`,
    # while the soft-delete/restore/activate actions inherited from BaseAdminCore
    # go away. See the class docstring for why soft delete is wrong here.
    actions = []

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return bool(request.user.is_superuser)

    def has_delete_permission(self, request, obj=None):
        return bool(request.user.is_superuser)

    def has_module_permission(self, request):
        return bool(request.user.is_superuser)
