from django.core.validators import (
    MaxLengthValidator,
    MaxValueValidator,
    MinLengthValidator,
    MinValueValidator,
    RegexValidator,
)
from django.db import models

from tenant.common.models.tenant_base_model import TenantBaseModel

# Slot generation is O(days x minutes_per_day / granularity x hosts) and runs
# synchronously inside the request. Both bounds below exist to keep that product
# finite: the field types alone allow max_horizon_days=32767 with a 1-minute
# granularity, which is ~47 million iterations per host per request — reachable
# by a tenant through the ordinary API, on themselves.
MAX_HORIZON_DAYS = 365
MIN_SLOT_GRANULARITY_MINUTES = 5


class EventTemplate(TenantBaseModel):
    """
    Defines a bookable appointment type (e.g. "30-Min Demo").
    Fully multi-tenant and audited (Tier 2).
    """

    class AssignmentStrategy(models.TextChoices):
        COLLECTIVE = "COLLECTIVE", "Collective"
        DIRECT = "DIRECT", "Direct"

    class LocationType(models.TextChoices):
        IN_PERSON = "IN_PERSON", "In Person"
        PHONE = "PHONE", "Phone"
        VIDEO = "VIDEO", "Video"

    name = models.CharField(
        max_length=100,
        validators=[
            MinLengthValidator(2),
            MaxLengthValidator(100),
            RegexValidator(
                regex=r"^[\w\s-]+$",
                message="Event name can only contain letters, numbers, spaces, hyphens, and underscores.",
            ),
        ],
        help_text="Human-readable name of the event type (e.g., '30-Min Demo').",
    )
    description = models.TextField(
        blank=True,
        default="",
        help_text="Optional description of what this event type is for.",
    )
    duration_minutes = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1)],
        help_text="Length of the appointment, in minutes.",
    )
    buffer_before_minutes = models.PositiveSmallIntegerField(
        default=0,
        help_text="Padding reserved before the appointment (minutes). Counts toward overlap checks.",
    )
    buffer_after_minutes = models.PositiveSmallIntegerField(
        default=0,
        help_text="Padding reserved after the appointment (minutes). Counts toward overlap checks.",
    )
    assignment_strategy = models.CharField(
        max_length=20,
        choices=AssignmentStrategy.choices,
        default=AssignmentStrategy.DIRECT,
        help_text=(
            "How hosts are assigned when booking this event type. DIRECT books the "
            "named host; COLLECTIVE books every skilled host as one meeting."
        ),
    )
    min_notice_minutes = models.PositiveIntegerField(
        default=120,
        help_text="Earliest a slot may be booked, as an offset from 'now' (minutes).",
    )
    max_horizon_days = models.PositiveSmallIntegerField(
        default=60,
        validators=[MinValueValidator(1), MaxValueValidator(MAX_HORIZON_DAYS)],
        help_text=(
            f"Furthest into the future a slot may be booked (days, max {MAX_HORIZON_DAYS})."
        ),
    )
    slot_granularity_minutes = models.PositiveSmallIntegerField(
        default=15,
        validators=[MinValueValidator(MIN_SLOT_GRANULARITY_MINUTES)],
        help_text=(
            "Step size between candidate start times when generating slots "
            f"(minutes, min {MIN_SLOT_GRANULARITY_MINUTES})."
        ),
    )
    cancellation_lead_minutes = models.PositiveIntegerField(
        default=60,
        help_text="Minimum notice required to cancel a booking (minutes before start).",
    )
    reschedule_lead_minutes = models.PositiveIntegerField(
        default=60,
        help_text="Minimum notice required to reschedule a booking (minutes before start).",
    )
    location_type = models.CharField(
        max_length=20,
        choices=LocationType.choices,
        default=LocationType.VIDEO,
        help_text="Where the appointment takes place.",
    )
    color = models.CharField(
        max_length=7,
        blank=True,
        null=True,
        help_text="Optional hex color (e.g. #00FF00) for UI rendering.",
    )

    class Meta:
        db_table = "core_event_templates"
        verbose_name = "Event Template"
        verbose_name_plural = "Event Templates"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["name", "tenant"],
                condition=models.Q(is_deleted=False),
                name="unique_active_event_template_tenant",
            ),
            models.CheckConstraint(
                condition=models.Q(duration_minutes__gt=0),
                name="event_template_duration_positive",
            ),
            # Enforced in the database as well as the serializer: these bound the
            # cost of a synchronous request, so a direct-ORM writer (management
            # command, data fix, future service) must not be able to bypass them.
            models.CheckConstraint(
                condition=models.Q(
                    slot_granularity_minutes__gte=MIN_SLOT_GRANULARITY_MINUTES
                ),
                name="event_template_granularity_min",
            ),
            models.CheckConstraint(
                condition=models.Q(max_horizon_days__gte=1)
                & models.Q(max_horizon_days__lte=MAX_HORIZON_DAYS),
                name="event_template_horizon_bounded",
            ),
        ]

    def soft_delete(self, user=None):
        """
        Truncation-safe soft delete: frees the unique (name, tenant) slot for
        reuse by suffixing the name with a deletion timestamp, trimming the base
        name first so the result never exceeds max_length.

        Refuses while active bookings still reference this type. PROTECT on the FK
        cannot cover this: templates are soft-deleted, so the database constraint
        never fires and customers would be left holding confirmed bookings for a
        meeting type that no longer exists.

        The check lives here rather than in the viewset so every path gets it —
        the REST delete, `bulk_delete` (which calls `soft_delete` directly and
        would otherwise skip it), and a shell session alike. It runs inside the
        lock below so a booking created concurrently cannot slip past it.
        """
        from django.core.exceptions import ValidationError
        from django.db import transaction
        from django.utils import timezone

        from core.models.booking import ACTIVE_BOOKING_STATUSES, Booking

        with transaction.atomic():
            EventTemplate.objects.select_for_update().get(pk=self.pk)

            blocking = Booking.objects.filter(
                event_template=self,
                is_deleted=False,
                status__in=ACTIVE_BOOKING_STATUSES,
            ).count()
            if blocking:
                raise ValidationError(
                    f"This event type has {blocking} active booking(s) and cannot be "
                    "deleted. Cancel or complete them first, or deactivate it "
                    "(is_active=false) to stop new bookings while honouring the ones "
                    "already made, once no active bot or bookable host still uses it."
                )

            self.is_deleted = True
            self.deleted_at = timezone.now()
            if user:
                self.deleted_by = user

            suffix = f"-deleted-{self.deleted_at.strftime('%Y%m%d%H%M%S')}"
            max_name_len = 100 - len(suffix)
            self.name = f"{self.name[:max_name_len]}{suffix}"

            self.save(
                update_fields=["is_deleted", "deleted_at", "name", "deleted_by"]
                if user
                else ["is_deleted", "deleted_at", "name"]
            )

    def deactivation_blockers(self):
        """What still depends on this type being active, as (bot names, host emails).

        Two things keep a type live. A bot that offers it (`Agent.event_templates`),
        and a host who can take it: an active skill for this type, held by someone with
        an active availability profile. Deactivating under either leaves a bot offering
        meetings that are all refused, or hosts whose calendars quietly stop producing
        slots for it.
        """
        bots = list(
            self.agents.filter(is_deleted=False, is_active=True)
            .order_by("bot_name")
            .values_list("bot_name", flat=True)
        )
        hosts = list(
            self.skills.filter(
                is_deleted=False,
                is_active=True,
                user__availability_profiles__is_deleted=False,
                user__availability_profiles__is_active=True,
            )
            .order_by("user__email")
            .values_list("user__email", flat=True)
            .distinct()
        )
        return bots, hosts

    def __str__(self):
        return self.name
