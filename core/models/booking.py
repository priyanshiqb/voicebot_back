from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import (
    DateTimeRangeField,
    RangeBoundary,
    RangeOperators,
)
from django.db import models
from django.db.models import Func, Q

from tenant.common.models.tenant_base_model import TenantBaseModel

# Statuses that occupy a host's time and therefore participate in overlap
# detection. Defined at module level so it is reachable from Booking.Meta
# (nested class bodies cannot see enclosing-class names).
ACTIVE_BOOKING_STATUSES = ["HELD", "SCHEDULED", "CONFIRMED", "REQUIRES_RESCHEDULE"]


class TsTzRange(Func):
    """Postgres tstzrange(lower, upper, bounds) expression for the exclusion constraint."""

    function = "TSTZRANGE"
    output_field = DateTimeRangeField()


class Booking(TenantBaseModel):
    """
    A single real appointment occurrence. Tier 2 (audited).

    Correctness note: two active bookings for the same host in the same tenant
    can never overlap — this is enforced at the database level by
    ``core_booking_no_overlap_per_host`` (a Postgres GiST exclusion constraint),
    not merely by serializer validation. Buffers are folded into
    ``effective_start_time`` / ``effective_end_time`` so the padding is enforced
    too. The half-open ``[)`` boundary means back-to-back bookings do not clash.
    """

    class Status(models.TextChoices):
        HELD = "HELD", "Held"
        SCHEDULED = "SCHEDULED", "Scheduled"
        CONFIRMED = "CONFIRMED", "Confirmed"
        COMPLETED = "COMPLETED", "Completed"
        CANCELED = "CANCELED", "Canceled"
        NO_SHOW = "NO_SHOW", "No Show"
        REQUIRES_RESCHEDULE = "REQUIRES_RESCHEDULE", "Requires Reschedule"
        RESCHEDULED = "RESCHEDULED", "Rescheduled"

    class Source(models.TextChoices):
        CUSTOMER = "CUSTOMER", "Customer"
        INTERNAL = "INTERNAL", "Internal"
        PUBLIC_LINK = "PUBLIC_LINK", "Public Link"
        AI = "AI", "AI Agent"

    # Convenience mirror of the module-level constant for service/test code.
    ACTIVE_STATUSES = ACTIVE_BOOKING_STATUSES

    event_template = models.ForeignKey(
        "core.EventTemplate",
        on_delete=models.PROTECT,
        related_name="bookings",
        help_text="Event type this booking is an instance of.",
    )
    title = models.CharField(
        max_length=255,
        help_text="Display title for the appointment.",
    )
    primary_host = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="hosted_bookings",
        help_text="The host responsible for this booking (drives overlap isolation).",
    )
    start_time = models.DateTimeField(help_text="Appointment start (UTC-aware).")
    end_time = models.DateTimeField(help_text="Appointment end (UTC-aware).")
    effective_start_time = models.DateTimeField(
        help_text="start_time minus buffer_before. Used by the overlap exclusion constraint.",
    )
    effective_end_time = models.DateTimeField(
        help_text="end_time plus buffer_after. Used by the overlap exclusion constraint.",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.SCHEDULED,
        db_index=True,
        help_text="Lifecycle status of the booking.",
    )
    source = models.CharField(
        max_length=20,
        choices=Source.choices,
        default=Source.INTERNAL,
        help_text="Where the booking originated.",
    )
    hold_expires_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="For HELD bookings: when the temporary reservation lapses.",
    )
    reschedule_token = models.UUIDField(
        blank=True,
        null=True,
        db_index=True,
        help_text="Opaque single-use token backing the public reschedule link.",
    )
    reschedule_token_expires_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="Expiry for reschedule_token.",
    )
    external_event_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="ID of the mirrored event in the host's external calendar (idempotency key).",
    )
    external_provider = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text="External calendar provider the event was mirrored to.",
    )
    group_id = models.UUIDField(
        blank=True,
        null=True,
        db_index=True,
        help_text="Ties the per-host rows of a COLLECTIVE booking together as one meeting.",
    )
    cancellation_reason = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Reason captured when the booking is canceled.",
    )

    class Meta:
        db_table = "core_bookings"
        verbose_name = "Booking"
        verbose_name_plural = "Bookings"
        ordering = ["-start_time"]
        indexes = [
            models.Index(
                fields=["tenant", "primary_host", "start_time"],
                name="core_booking_host_start_idx",
            ),
            models.Index(
                fields=["tenant", "status", "start_time"],
                name="core_booking_status_start_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_time__gt=models.F("start_time")),
                name="booking_end_after_start",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    effective_end_time__gt=models.F("effective_start_time")
                ),
                name="booking_effective_end_after_start",
            ),
            ExclusionConstraint(
                name="core_booking_no_overlap_per_host",
                expressions=[
                    ("tenant", RangeOperators.EQUAL),
                    ("primary_host", RangeOperators.EQUAL),
                    (
                        TsTzRange(
                            "effective_start_time",
                            "effective_end_time",
                            RangeBoundary(),
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                ],
                condition=Q(is_deleted=False) & Q(status__in=ACTIVE_BOOKING_STATUSES),
            ),
        ]

    def __str__(self):
        return f"{self.title} @ {self.start_time:%Y-%m-%d %H:%M} ({self.status})"
