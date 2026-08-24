from django.contrib import admin
from django.utils import timezone

from tenant.common.admin.tenant_base_admin_core import TenantBaseAdminCore
from wallet.models.credit_ledger import CreditLedger
from wallet.models.wallet import Wallet


class CreditLedgerInline(admin.TabularInline):
    """The last 20 movements, in place. Read-only, like the ledger itself.

    A balance without its history is unactionable — the first question anyone opens a
    wallet to ask is "why is it that number", and this answers it without a second
    page and a filter.
    """

    model = CreditLedger
    extra = 0
    max_num = 0
    can_delete = False
    fields = ("created_at", "entry_type", "bucket", "amount", "balance_after", "reference_id")
    readonly_fields = fields
    ordering = ("-created_at",)
    verbose_name_plural = "Recent movements"

    def get_queryset(self, request):
        return super().get_queryset(request)[:20]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Wallet)
class WalletAdmin(TenantBaseAdminCore):
    """
    A tenant's credit balance.

    **The balance fields are read-only, deliberately.** They are a cache of the
    ledger, and editing one here would break the invariant silently — the number
    would change while the history that explains it did not. Credits move by posting
    a ledger entry, never by typing a new total.

    Grant and Deduct actions land with the wallet service (W2), which is the only
    thing allowed to write a balance and its ledger row together under a row lock.
    Adding them here first would create exactly the second writer the design exists
    to prevent.

    Freeze and unfreeze **are** here: they set a flag, touch no balance, and write no
    ledger row, so support can suspend a tenant without waiting for the service.
    """

    model = Wallet
    inlines = [CreditLedgerInline]

    list_display = (
        "tenant",
        "balance",
        "allocated_balance",
        "purchased_balance",
        "is_frozen",
        "current_plan",
        "low_balance_display",
        "updated_at",
    )
    list_filter = ("is_frozen",) + TenantBaseAdminCore.list_filter
    search_fields = (
        "tenant__name",
        "tenant__subdomain",
    ) + TenantBaseAdminCore.search_fields
    list_select_related = ("tenant", "current_plan")
    ordering = ("-balance",)

    readonly_fields = TenantBaseAdminCore.readonly_fields + (
        "balance",
        "allocated_balance",
        "purchased_balance",
        "lifetime_granted",
        "lifetime_consumed",
        "is_frozen",
        "frozen_reason",
        "frozen_at",
        "current_plan",
        "rate_snapshot",
        "rate_snapshot_at",
    )

    actions = ("freeze_wallets", "unfreeze_wallets")

    fieldsets = (
        ("Balance", {
            "fields": ("id", "tenant", "balance", "allocated_balance", "purchased_balance"),
            "description": (
                "Read-only. Allocated is what a plan cycle granted and what rollover "
                "rules can burn; purchased was bought with cash and always survives. "
                "Consumption drains allocated first."
            ),
        }),
        ("Spendability", {"fields": ("is_frozen", "frozen_reason", "frozen_at")}),
        ("Rate card in force", {
            "fields": ("current_plan", "rate_snapshot", "rate_snapshot_at"),
            "description": (
                "Stamped from the plan when a subscription activates, renews or "
                "changes. This — not the CreditRate table — is what prices a call."
            ),
        }),
        ("Alerting", {"fields": ("low_balance_threshold", "low_balance_notified_at")}),
        ("Lifetime", {
            "fields": ("lifetime_granted", "lifetime_consumed"),
            "classes": ("collapse",),
        }),
        ("Timestamps", {"fields": ("created_at", "updated_at", "deleted_at")}),
    )

    @admin.display(description="Low balance", boolean=True)
    def low_balance_display(self, obj):
        return obj.is_low

    @admin.action(description="Freeze — block spending")
    def freeze_wallets(self, request, queryset):
        updated = queryset.filter(is_frozen=False).update(
            is_frozen=True,
            frozen_reason=f"admin:{request.user}",
            frozen_at=timezone.now(),
        )
        self.message_user(request, f"Froze {updated} wallet(s). Grants still land; spending is blocked.")

    @admin.action(description="Unfreeze — allow spending")
    def unfreeze_wallets(self, request, queryset):
        updated = queryset.filter(is_frozen=True).update(
            is_frozen=False, frozen_reason=None, frozen_at=None
        )
        self.message_user(request, f"Unfroze {updated} wallet(s).")
