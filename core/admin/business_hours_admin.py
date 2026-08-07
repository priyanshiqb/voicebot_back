from django.contrib import admin

from core.models.business_hours import BusinessHours, BusinessHoursInterval
from tenant.common.admin.tenant_base_admin import TenantBaseAdmin
from tenant.common.admin.tenant_base_admin_core import TenantBaseAdminCore


class BusinessHoursIntervalInline(admin.TabularInline):
    """Intervals are edited on the schedule they belong to.

    A weekly grid read one row at a time is unreadable — you cannot see that
    Tuesday is missing, which is the mistake this screen exists to catch.
    """

    model = BusinessHoursInterval
    extra = 0
    fields = ("weekday", "opens_at", "closes_at")
    ordering = ("weekday", "opens_at")

    def get_formset(self, request, obj=None, **kwargs):
        """Stamp `tenant` from the parent schedule.

        `BusinessHoursInterval.tenant` is non-null and the inline form does not
        show it, so without this every save through the admin raises IntegrityError.
        """
        formset = super().get_formset(request, obj, **kwargs)
        parent = obj

        class TenantStampedFormSet(formset):
            def save_new(self, form, commit=True):
                instance = super().save_new(form, commit=False)
                if parent is not None:
                    instance.tenant_id = parent.tenant_id
                if commit:
                    instance.save()
                return instance

        return TenantStampedFormSet


@admin.register(BusinessHours)
class BusinessHoursAdmin(TenantBaseAdmin):
    model = BusinessHours

    list_display = (
        "tenant",
        "timezone",
        "is_open",
        "open_days",
        "is_active",
        "updated_at",
    )
    list_filter = ("is_open",) + TenantBaseAdmin.list_filter
    search_fields = ("tenant__name", "tenant__subdomain", "timezone__timezone")
    ordering = ("-updated_at",)
    list_select_related = ("tenant", "timezone")
    inlines = (BusinessHoursIntervalInline,)

    @admin.display(description="Open days")
    def open_days(self, obj):
        """How many weekdays have any hours — the fastest read on a schedule.

        Zero is worth spotting: with `is_open` true and no intervals the business
        is closed all week, which looks configured until you count.
        """
        weekdays = {interval.weekday for interval in obj.intervals.all()}
        return f"{len(weekdays)}/7"


@admin.register(BusinessHoursInterval)
class BusinessHoursIntervalAdmin(TenantBaseAdminCore):
    """Registered separately for search and bulk work, but the inline above is the
    normal way in."""

    model = BusinessHoursInterval

    list_display = ("business_hours", "weekday", "opens_at", "closes_at", "created_at")
    list_filter = ("weekday",) + TenantBaseAdminCore.list_filter
    ordering = ("business_hours", "weekday", "opens_at")
    list_select_related = ("business_hours",)
    raw_id_fields = ("business_hours",)
