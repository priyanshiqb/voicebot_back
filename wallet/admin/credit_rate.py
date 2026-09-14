from datetime import timedelta

from django.contrib import admin
from django.utils import timezone

from utils.admin.base_admin_core import BaseAdminCore
from wallet.models.credit_rate import CreditRate


class CreditRateInline(admin.TabularInline):
    """Rates edited on the plan they belong to, where the numbers mean something.

    "3 credits a minute" is a sentence about a plan; read on its own it is trivia.
    """

    model = CreditRate
    extra = 0
    fields = ("operation", "unit", "credits_per_unit", "price_per_minute_inr", "rounding",
              "minimum_units", "effective_from", "effective_to")
    ordering = ("operation", "-effective_from")


@admin.register(CreditRate)
class CreditRateAdmin(BaseAdminCore):
    """
    The per-plan price of a minute, versioned.

    **Rates are superseded, never edited.** Changing `credits_per_unit` in place
    rewrites what a plan advertised historically, and the answer to "when did Pro go
    to 3 credits a minute" disappears. Use the supersede action instead: it closes
    the current row and leaves you to add its replacement.

    The database enforces the half that matters — at most one open-ended rate per
    (plan, operation) — so publishing a new price without closing the old one fails
    rather than producing two current rates and a coin-flip at stamp time.
    """

    model = CreditRate

    list_display = (
        "plan",
        "operation",
        "price_display",
        "minimum_units",
        "effective_from",
        "effective_to",
        "is_current_display",
    )
    list_filter = ("operation", "unit", "plan") + BaseAdminCore.list_filter
    search_fields = ("plan__code", "plan__name", "operation", "notes") + BaseAdminCore.search_fields
    list_select_related = ("plan",)
    ordering = ("plan", "operation", "-effective_from")

    actions = ("supersede_rates",)

    fieldsets = (
        ("Applies to", {"fields": ("id", "plan", "operation", "is_active")}),
        ("Price", {
            "fields": ("unit", "credits_per_unit", "price_per_minute_inr", "rounding",
                       "minimum_units"),
            "description": (
                "Credits are whole numbers, so a per-minute price is a whole number of "
                "credits. Rounding and minimum_units decide what a partial unit costs: "
                "with ceil and a minimum of 1, a 4-second call bills one minute. "
                "price_per_minute_inr is what a minute costs in rupees when the plan is "
                "sold at this tier; it is never read on a call."
            ),
        }),
        ("Validity", {
            "fields": ("effective_from", "effective_to", "notes"),
            "description": "Leave effective_to blank for the current rate.",
        }),
        ("Timestamps", {"fields": ("created_at", "updated_at", "deleted_at")}),
    )

    @admin.display(description="Price")
    def price_display(self, obj):
        return f"{obj.credits_per_unit} cr / {obj.get_unit_display().lower()}"

    @admin.display(description="Current", boolean=True)
    def is_current_display(self, obj):
        return obj.is_current

    @admin.action(description="Supersede — close these rates as of now")
    def supersede_rates(self, request, queryset):
        """Close each selected open-ended rate so a replacement can be created.

        ``effective_to`` is clamped to just after ``effective_from`` rather than set
        blindly to now, because a zero-length validity window is rejected by
        ``ck_credit_rate_period_valid`` — correctly, since such a row was never in
        force. That only bites on a rate created moments ago, but a supersede action
        that raises an IntegrityError on the fast path is worse than one that adds a
        microsecond.

        For a rate published by mistake, soft-delete is the right tool instead: the
        uniqueness constraint ignores deleted rows, so the slot frees immediately and
        a corrected rate can take its place in the same second.
        """
        now = timezone.now()
        closed = skipped = 0

        for rate in queryset:
            if not rate.is_current:
                skipped += 1
                continue
            rate.effective_to = max(now, rate.effective_from + timedelta(microseconds=1))
            rate.save(update_fields=["effective_to"])
            closed += 1

        message = f"Closed {closed} rate(s). Add the replacement rows now — until you do, those plans have no price for that operation."
        if skipped:
            message += f" Skipped {skipped} already-superseded row(s)."
        self.message_user(request, message)
