from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError
from django.db import models

from utils.models.base_model_core import BaseModelCore

UTC = ZoneInfo("UTC")


def get_default_timezone():
    """
    Failsafe callable default to retrieve the UTC timezone database instance.
    Returns None if the database hasn't been seeded yet.
    """
    from onboard.models.timezone import Timezone
    try:
        obj = Timezone.objects.filter(timezone="UTC").first()
        return obj.pk if obj else None
    except Exception:
        return None


def validate_iana_timezone(value):
    """
    Reject anything ``zoneinfo`` cannot resolve.

    Write-time rejection matters because the read-time failure is a 500 several
    layers away — the availability engine resolves this string for every host in
    the zone, and an unknown key raises there rather than here.
    """
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError(
            f"{value!r} is not a valid IANA timezone identifier."
        ) from exc


def _transition_after(naive, tz):
    """
    Exact UTC instant of the DST transition that makes ``naive`` nonexistent.

    PEP 495: ``fold=1`` interprets the value with the POST-transition offset,
    landing BEFORE the gap; ``fold=0`` uses the PRE-transition offset, landing
    AFTER it. The transition is bracketed between the two — bisect to the second.

    Do NOT replace this with "use fold=0 and accept it", and do NOT substitute
    ``dateutil.tz.resolve_imaginary``: both shift by the gap WIDTH rather than to
    the gap's END. For a single instant that is harmless, but for an interval it
    collides with an end time just past the gap and yields a ZERO-LENGTH result.
    Regression case: 02:30 -> 03:30 on 2026-03-08 in America/New_York.
    """
    lo = naive.replace(tzinfo=tz, fold=1).astimezone(UTC)
    hi = naive.replace(tzinfo=tz, fold=0).astimezone(UTC)
    lo_offset = lo.astimezone(tz).utcoffset()
    while hi - lo > timedelta(seconds=1):
        mid = lo + (hi - lo) / 2
        if mid.astimezone(tz).utcoffset() == lo_offset:
            lo = mid
        else:
            hi = mid
    return hi


def _resolve_local(day, wall_time, tz, *, prefer_fold=0):
    """
    Naive local ``(date, time)`` -> aware datetime, resolving DST edges.

    Returns ``(aware_datetime, flag)`` where flag is
    ``"" | "nonexistent" | "ambiguous"``.

      * NONEXISTENT (spring forward) — the value does not survive a UTC
        round-trip. Snap to the first valid instant, i.e. the transition itself.
      * AMBIGUOUS (fall back) — two valid offsets. ``prefer_fold`` selects which.
    """
    naive = datetime.combine(day, wall_time)
    dt = naive.replace(tzinfo=tz, fold=prefer_fold)

    if dt.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != naive:
        return _transition_after(naive, tz), "nonexistent"

    if dt.replace(fold=0).utcoffset() != dt.replace(fold=1).utcoffset():
        return dt, "ambiguous"

    return dt, ""


class Timezone(BaseModelCore):
    """
    An IANA timezone, and the single place wall-clock <-> UTC conversion happens.

    ``offset`` is a STANDARD-TIME DISPLAY STRING and must never be used for
    arithmetic: an offset is a property of a place *at a moment*, not of a place.
    Roughly a third of real zones change offset twice a year. Use
    ``utc_offset_at()`` / ``offset_label_at()`` for the value actually in force,
    and ``to_utc()`` to convert a local wall-clock time.
    """

    offset = models.CharField(
        max_length=50,
        help_text=(
            "Standard-time UTC offset for display only (e.g., UTC-12:00). NOT "
            "DST-aware — use offset_label_at() for the offset in force at a "
            "given instant."
        ),
    )
    region = models.CharField(
        max_length=255,
        help_text="Associated regions/cities (e.g., London, Dublin)"
    )
    timezone = models.CharField(
        max_length=100,
        unique=True,
        validators=[validate_iana_timezone],
        help_text="Standard IANA timezone identifier (e.g., Europe/London)"
    )

    class Meta(BaseModelCore.Meta):
        db_table = "onboard_timezone"
        verbose_name = "Timezone"
        verbose_name_plural = "Timezones"
        ordering = ["timezone"]

    @property
    def zone(self):
        """
        The IANA zone object.

        A plain property rather than ``cached_property``: ``ZoneInfo`` already
        caches by key at module level, so this is a dict lookup, and a cached
        attribute would go stale if ``self.timezone`` were reassigned.
        """
        return ZoneInfo(self.timezone)

    # --- offset queries ------------------------------------------------------

    def utc_offset_at(self, instant):
        """The real UTC offset at an aware instant — what ``offset`` only approximates."""
        return instant.astimezone(self.zone).utcoffset()

    def offset_label_at(self, instant):
        """``'UTC+05:30'``-style label for the offset in force at ``instant``."""
        total = int(self.utc_offset_at(instant).total_seconds())
        sign = "+" if total >= 0 else "-"
        hours, minutes = divmod(abs(total) // 60, 60)
        return f"UTC{sign}{hours:02d}:{minutes:02d}"

    def is_dst_at(self, instant):
        """Whether summer time is in force at ``instant``."""
        return bool(instant.astimezone(self.zone).dst())

    def standard_offset(self, year=None):
        """
        The non-DST offset, as the minimum offset observed across ``year``.

        Hemisphere-agnostic: DST always ADDS to standard time, so the yearly
        minimum is standard time everywhere. Used by the drift audit for the
        display column; never by the scheduling engine.
        """
        from django.utils import timezone as dj_tz

        year = year or dj_tz.now().year
        return min(
            self.utc_offset_at(datetime(year, month, 15, 12, tzinfo=UTC))
            for month in range(1, 13)
        )

    # --- the conversion that matters -----------------------------------------

    def to_utc(self, day, wall_time, *, prefer_fold=0):
        """
        Naive local ``(date, time)`` in THIS zone -> ``(aware UTC datetime, flag)``.

        ``prefer_fold`` selects which occurrence of an AMBIGUOUS local time is
        meant: 0 = the first (DST still in force), 1 = the second. When
        converting an interval, pass 0 for its start and 1 for its end so the
        interval covers real elapsed time across a fall-back night.

        ``flag`` is ``"" | "nonexistent" | "ambiguous"``. Persist it on derived
        rows so a surprising duration is explainable from the database.
        """
        aware, flag = _resolve_local(day, wall_time, self.zone, prefer_fold=prefer_fold)
        return aware.astimezone(UTC), flag

    def __str__(self):
        return f"{self.timezone} ({self.offset})"
