from django import forms
from django.contrib import admin
from django.contrib.admin.helpers import ActionForm
from django.utils import timezone

from django.forms.models import BaseInlineFormSet

from tenant.common.admin.tenant_base_admin_core import TenantBaseAdminCore
from wallet.models.credit_ledger import CreditLedger
from wallet.models.wallet import Wallet


class CreditLedgerFormSet(BaseInlineFormSet):
    def get_queryset(self):
        qs = super().get_queryset()
        return qs[:20]


class CreditLedgerInline(admin.TabularInline):
    """The last 20 movements, in place. Read-only, like the ledger itself.

    A balance without its history is unactionable — the first question anyone opens a
    wallet to ask is "why is it that number", and this answers it without a second
    page and a filter.
    """

    model = CreditLedger
    formset = CreditLedgerFormSet
    extra = 0
    max_num = 0
    can_delete = False
    fields = ("created_at", "entry_type", "bucket", "amount", "balance_after", "reference_id")
    readonly_fields = fields
    ordering = ("-created_at",)
    verbose_name_plural = "Recent movements"

    def has_add_permission(self, request, obj=None):
        return False


class CreditAdjustmentForm(ActionForm):
    """Extra fields on the action bar, so a grant needs an amount and a reason.

    An ActionForm rather than an intermediate page: it keeps the whole interaction on
    one screen, and there is nothing here worth a confirmation step that a required
    reason field does not already provide.
    """

    adjustment_amount = forms.IntegerField(
        required=False, min_value=1, label="Credits",
        widget=forms.NumberInput(attrs={"placeholder": "e.g. 500", "style": "width:8em"}),
    )
    adjustment_reason = forms.CharField(
        required=False, label="Reason / ticket",
        widget=forms.TextInput(attrs={"placeholder": "TICKET-123 — goodwill", "style": "width:22em"}),
    )


@admin.register(Wallet)
class WalletAdmin(TenantBaseAdminCore):
    """
    A tenant's credit balance.

    **The balance fields are read-only, deliberately.** They are a cache of the
    ledger, and editing one here would break the invariant silently — the number
    would change while the history that explains it did not. Credits move by posting
    a ledger entry, never by typing a new total.

    **Grant and Deduct go through ``wallet.services.wallet.adjust()``**, never through
    a field. The service takes the row lock, writes the ledger entry and keeps the two
    buckets consistent in one transaction; doing any of that here would make the admin
    a second writer, which is the one thing the design forbids. Both require a reason —
    an unexplained balance change is worse than none.

    Freeze and unfreeze set a flag, touch no balance and write no ledger row, so they
    stay local.
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

    action_form = CreditAdjustmentForm
    actions = ("grant_credits", "deduct_credits", "freeze_wallets", "unfreeze_wallets")

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

    # --- credit movement, through the service --------------------------------

    def _adjustment_args(self, request):
        """The amount and reason from the action bar, or an error string."""
        raw = (request.POST.get("adjustment_amount") or "").strip()
        reason = (request.POST.get("adjustment_reason") or "").strip()
        if not raw:
            return None, None, "Enter a number of credits."
        if not reason:
            return None, None, "Enter a reason — an unexplained balance change is worse than none."
        try:
            amount = int(raw)
        except ValueError:
            return None, None, f"'{raw}' is not a whole number of credits."
        if amount < 1:
            return None, None, "Credits must be a positive whole number."
        return amount, reason, None

    def _apply_adjustment(self, request, queryset, sign):
        """Move credits via wallet.services.wallet.adjust().

        Never touches a balance directly. The service takes the row lock, writes the
        ledger entry and keeps the buckets consistent — doing any of that here would
        make the admin a second writer, which is exactly what the design forbids.
        """
        from wallet.services import wallet as svc

        amount, reason, error = self._adjustment_args(request)
        if error:
            self.message_user(request, error, level="ERROR")
            return

        # Second-precision timestamp: a double-submitted form inside the same second
        # is absorbed by the ledger's idempotency constraint, while a deliberate
        # repeat a minute later still works.
        stamp = timezone.now().strftime("%Y%m%d%H%M%S")
        moved = blocked = 0

        for wallet in queryset:
            try:
                entry = svc.adjust(
                    wallet.tenant_id,
                    amount=sign * amount,
                    reference_id=f"admin:{wallet.pk}:{stamp}",
                    performed_by=request.user,
                    notes=reason,
                )
            except Exception as exc:  # noqa: BLE001 — surfaced to the operator below
                self.message_user(request, f"{wallet.tenant}: {exc}", level="ERROR")
                blocked += 1
                continue
            if entry is None:
                blocked += 1
            else:
                moved += 1

        verb = "Granted" if sign > 0 else "Deducted"
        message = f"{verb} {amount} credit(s) on {moved} wallet(s)."
        if blocked:
            message += f" {blocked} skipped — already applied, or nothing left to deduct."
        self.message_user(request, message)

    @admin.action(description="Grant credits (enter amount + reason above)")
    def grant_credits(self, request, queryset):
        self._apply_adjustment(request, queryset, sign=1)

    @admin.action(description="Deduct credits (enter amount + reason above)")
    def deduct_credits(self, request, queryset):
        self._apply_adjustment(request, queryset, sign=-1)
