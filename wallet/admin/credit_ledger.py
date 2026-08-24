from django.contrib import admin

from utils.admin.base_admin_core import BaseAdminCore
from wallet.models.credit_ledger import CreditLedger


@admin.register(CreditLedger)
class CreditLedgerAdmin(BaseAdminCore):
    """
    The credit ledger, strictly read-only.

    ``Wallet.balance`` is a cache of ``SUM(amount)`` over these rows, which only means
    something if the rows never change. The model and its queryset both refuse writes;
    this closes the third door. Add, change and delete are all off, and the soft-delete
    and activate/deactivate actions inherited from ``BaseAdminCore`` are dropped —
    every one of them would raise from the model anyway, so leaving them on the page
    only offers staff a button that errors.

    A mistake is corrected by posting a compensating ``ADMIN_ADJUSTMENT`` entry, which
    is a Wallet action rather than anything here.
    """

    model = CreditLedger
    actions = None

    list_display = (
        "created_at",
        "tenant",
        "entry_type",
        "bucket",
        "amount",
        "balance_after",
        "operation",
        "rating_display",
        "reference_id",
    )
    list_filter = ("entry_type", "bucket", "operation", "created_at")
    search_fields = (
        "reference_id",
        "wallet__id",
        "tenant__name",
        "tenant__subdomain",
        "notes",
    ) + BaseAdminCore.search_fields
    list_select_related = ("tenant", "wallet")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    fieldsets = (
        ("Movement", {
            "fields": ("id", "tenant", "wallet", "entry_type", "bucket", "amount", "balance_after"),
        }),
        ("How it was rated", {
            "fields": ("operation", "raw_quantity", "units", "unit_type", "rate_applied", "rate"),
            "description": (
                "Present on usage rows only. This is what answers “why was I charged "
                "this much?” — and it stays correct after prices change, because the "
                "numbers are copied here rather than looked up."
            ),
        }),
        ("Provenance", {
            "fields": ("reference_id", "subscription", "performed_by", "notes", "metadata"),
            "description": (
                "reference_id is the idempotency key: unique per entry type, which is "
                "what makes a replayed webhook or a re-posted call a no-op."
            ),
        }),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Rating")
    def rating_display(self, obj):
        if obj.units is None:
            return "—"
        return f"{obj.raw_quantity} → {obj.units} × {obj.rate_applied}"
