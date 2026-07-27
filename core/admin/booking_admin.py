from django.contrib import admin

from tenant.common.admin.tenant_base_admin import TenantBaseAdmin
from core.models.booking import Booking
from core.models.booking_event import BookingEvent
from core.models.booking_participant import BookingParticipant


class BookingParticipantInline(admin.TabularInline):
    model = BookingParticipant
    extra = 0
    can_delete = False
    fields = ("name", "email", "phone_number", "role", "response_status", "user")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class BookingEventInline(admin.TabularInline):
    model = BookingEvent
    extra = 0
    can_delete = False
    fields = ("event_type", "actor", "from_host", "to_host", "metadata", "created_at")
    readonly_fields = fields
    ordering = ("-created_at",)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Booking)
class BookingAdmin(TenantBaseAdmin):
    model = Booking

    list_display = (
        "id",
        "title",
        "tenant",
        "primary_host",
        "start_time",
        "end_time",
        "status",
        "source",
        "created_at",
    )
    list_filter = ("status", "source") + TenantBaseAdmin.list_filter
    search_fields = ("title", "id") + TenantBaseAdmin.search_fields
    ordering = ("-start_time",)
    readonly_fields = TenantBaseAdmin.readonly_fields + (
        "effective_start_time",
        "effective_end_time",
        "external_event_id",
        "external_provider",
        "reschedule_token",
        "reschedule_token_expires_at",
    )
    inlines = [BookingParticipantInline, BookingEventInline]
