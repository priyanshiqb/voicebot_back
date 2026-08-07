from datetime import datetime, timedelta

from django.db import models

from tenant.common.models.tenant_base_model import TenantBaseModel
from tenant.common.models.tenant_base_model_core import TenantBaseModelCore


class BusinessHours(TenantBaseModel):
    """
    When the **business** is open. One per tenant. Tier 2 (audited — changing a
    company's opening hours is worth a `updated_by`).

    **Not** :class:`core.AvailabilityProfile`, which is when a **person** is free.
    The two legitimately disagree: a voicebot answers around the clock while staff
    work nine to five, and a host on leave does not close the business. Separate
    tables because they have separate owners, separate lifecycles and separate
    consumers — but the time semantics below are copied from ``AvailabilityRule``
    deliberately, so anyone who knows one already knows the other.

    Resolution order, highest priority first — see :meth:`is_open_at`:

    1. ``is_open`` — the master switch
    2. the :class:`BusinessHoursInterval` rows for that weekday

    **Date-specific overrides are deliberately not modelled yet.** Public holidays,
    a one-off stocktake and early-closing days all belong here eventually, and the
    shape is known — dated rows that *replace* a weekday's intervals rather than
    merging with them. They are left out because nothing needs them today, not
    because they were overlooked. :meth:`intervals_for` takes a date rather than a
    weekday so that adding them later is a change in one method.
    """

    tenant = models.OneToOneField(
        "tenant.Tenant",
        on_delete=models.CASCADE,
        related_name="business_hours",
        help_text="The tenant whose opening hours these are.",
    )
    timezone = models.ForeignKey(
        "onboard.Timezone",
        on_delete=models.PROTECT,
        related_name="business_hours",
        help_text=(
            "IANA timezone the wall-clock times are expressed in (DST-aware). A "
            "ForeignKey rather than a `pytz.all_timezones` choices list: baking 500 "
            "entries into `choices` writes the tz database into a migration and "
            "regenerates one every time it is updated."
        ),
    )
    is_open = models.BooleanField(
        default=True,
        help_text=(
            "Master switch. False closes the business regardless of the schedule — "
            "for an indefinite shutdown, without deleting hours that will be wanted "
            "again."
        ),
    )
    closed_message = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Optional line for the agent to use out of hours.",
    )

    class Meta:
        db_table = "core_business_hours"
        verbose_name = "Business Hours"
        verbose_name_plural = "Business Hours"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Business hours ({self.tenant_id})"

    # --- resolution -----------------------------------------------------------

    def intervals_for(self, on_date):
        """The open intervals on ``on_date`` as ``[(opens_at, closes_at), …]``.

        Takes a date rather than a weekday on purpose: date-specific overrides
        will be resolved here when they arrive, ahead of the weekly pattern, and
        every caller already asks the question in the right terms.
        """
        return [
            (interval.opens_at, interval.closes_at)
            for interval in self.intervals.all()
            if interval.weekday == on_date.weekday()
        ]

    def is_open_at(self, moment):
        """Whether the business is open at ``moment`` (an aware datetime).

        Checks the **previous** day as well as the current one, because an
        interval that wraps midnight belongs to the day it started on: 02:00 on a
        Sunday is covered by Saturday's 22:00–02:00, and looking only at Sunday
        would report closed.
        """
        if not self.is_open:
            return False

        local = moment.astimezone(self.timezone.zone)
        for day_offset in (0, -1):
            on_date = (local + timedelta(days=day_offset)).date()
            for opens_at, closes_at in self.intervals_for(on_date):
                start = datetime.combine(on_date, opens_at, tzinfo=local.tzinfo)
                end = datetime.combine(on_date, closes_at, tzinfo=local.tzinfo)
                if closes_at <= opens_at:
                    end += timedelta(days=1)
                if start <= local < end:
                    return True
        return False


class BusinessHoursInterval(TenantBaseModelCore):
    """
    One open block on one weekday. Tier 1 Core.

    **Several rows per weekday are allowed, and that is the point** — a split
    shift (09:00–13:00, 14:00–18:00) is the normal shape for clinics, restaurants
    and anywhere that closes for lunch. A `unique_together` on
    ``(business_hours, weekday)`` would forbid it permanently, which is the single
    most consequential thing to get right here.

    A **closed day is the absence of rows**, not a row with a status flag and null
    times. One representation instead of two: nothing can be closed-with-hours or
    open-with-no-hours.

    ``opens_at`` / ``closes_at`` are naive wall-clock times read in the parent's
    timezone. When ``closes_at <= opens_at`` the block ends on the NEXT day, so
    22:00 → 02:00 is an overnight shift and 00:00 → 00:00 is a full 24 hours. A
    zero-length block is therefore not expressible, by design — same rule as
    ``AvailabilityRule``.
    """

    class Weekday(models.IntegerChoices):
        MONDAY = 0, "Monday"
        TUESDAY = 1, "Tuesday"
        WEDNESDAY = 2, "Wednesday"
        THURSDAY = 3, "Thursday"
        FRIDAY = 4, "Friday"
        SATURDAY = 5, "Saturday"
        SUNDAY = 6, "Sunday"

    business_hours = models.ForeignKey(
        "core.BusinessHours",
        on_delete=models.CASCADE,
        related_name="intervals",
        help_text="Schedule this block belongs to.",
    )
    weekday = models.PositiveSmallIntegerField(
        choices=Weekday.choices,
        help_text="Day of week (0=Monday … 6=Sunday, matching Python's weekday()).",
    )
    opens_at = models.TimeField(help_text="Local wall-clock opening time.")
    closes_at = models.TimeField(
        help_text=(
            "Local wall-clock closing time. If <= opens_at the block ends on the "
            "following day (overnight)."
        ),
    )

    class Meta:
        db_table = "core_business_hours_intervals"
        verbose_name = "Business Hours Interval"
        verbose_name_plural = "Business Hours Intervals"
        ordering = ["weekday", "opens_at"]
        # No unique on (business_hours, weekday) — that is what allows split shifts.
        # No closes_at > opens_at check either: an overnight block legitimately has
        # closes_at < opens_at.
        indexes = [models.Index(fields=["business_hours", "weekday"])]

    @property
    def wraps_midnight(self):
        return self.closes_at <= self.opens_at

    def __str__(self):
        suffix = " (+1d)" if self.wraps_midnight else ""
        return f"{self.get_weekday_display()} {self.opens_at}-{self.closes_at}{suffix}"
