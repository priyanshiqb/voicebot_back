"""
Availability engine.

Computes bookable slots and free hosts for an EventTemplate. All queries are
explicitly tenant-scoped (this code runs outside the DRF request layer, which
provides no automatic tenant filtering). Wall-clock working hours are resolved
through the profile's IANA timezone with ``zoneinfo`` so results are DST-correct.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from core.models.availability_exception import AvailabilityException
from core.models.availability_profile import AvailabilityProfile
from core.models.booking import ACTIVE_BOOKING_STATUSES, Booking
from core.models.user_event_skill import UserEventSkill

logger = logging.getLogger(__name__)

UTC = ZoneInfo("UTC")


@dataclass(frozen=True)
class Slot:
    start: datetime  # UTC-aware
    end: datetime  # UTC-aware
    eligible_host_ids: tuple  # host ids free for this slot


def skilled_host_ids(tenant, event_template):
    """Host ids configured (via UserEventSkill) to host this event type, in-tenant."""
    return list(
        UserEventSkill.objects.filter(
            tenant=tenant, event_template=event_template, is_deleted=False
        ).values_list("user_id", flat=True)
    )


def is_host_skilled(tenant, event_template, host_id):
    """True if this host is configured (via UserEventSkill) for this event type."""
    if not host_id:
        return False
    return UserEventSkill.objects.filter(
        tenant=tenant, event_template=event_template, user_id=host_id, is_deleted=False
    ).exists()


def _resolve_host_ids(tenant, event_template, target_host_id):
    """Hosts to consider for this template.

    A caller-supplied ``target_host_id`` is still checked against UserEventSkill:
    since every non-collective booking now names its host, an unchecked target
    would be a way to book *any* user — including one from another tenant — as
    host for an event type they were never configured for.
    """
    skilled = skilled_host_ids(tenant, event_template)
    if target_host_id is None:
        return skilled
    # Callers pass a UUID or a str (the AI tool layer hands us JSON strings).
    # Match on str, but return the canonical UUID — downstream lookups key the
    # profile map by UUID, and a str would silently miss every host.
    by_str = {str(h): h for h in skilled}
    match = by_str.get(str(target_host_id))
    return [match] if match is not None else []


def _load_profiles(tenant, host_ids):
    """Return {host_id: (profile, [rules])} for the given hosts, tenant-scoped."""
    profiles = (
        AvailabilityProfile.objects.filter(
            tenant=tenant, user_id__in=host_ids, is_deleted=False
        )
        .select_related("timezone")
        .prefetch_related("rules")
    )
    return {p.user_id: (p, list(p.rules.all())) for p in profiles}


def _within_working_hours(profile, rules, start_utc, end_utc):
    """True if [start, end] fits inside a single working block on its local day."""
    tz = profile.timezone.zone
    local_start = start_utc.astimezone(tz)
    local_end = end_utc.astimezone(tz)
    if local_start.date() != local_end.date():
        return False  # Phase 1: blocks never cross local midnight
    weekday = local_start.weekday()
    st, et = local_start.time(), local_end.time()
    for rule in rules:
        if rule.weekday == weekday and rule.start_time <= st and et <= rule.end_time:
            return True
    return False


def _busy_intervals(tenant, host_id, window_start, window_end, exclude_booking_id=None):
    """Effective (buffer-inclusive) busy intervals for a host within a window."""
    qs = Booking.objects.filter(
        tenant=tenant,
        primary_host_id=host_id,
        is_deleted=False,
        status__in=ACTIVE_BOOKING_STATUSES,
        effective_start_time__lt=window_end,
        effective_end_time__gt=window_start,
    )
    if exclude_booking_id:
        qs = qs.exclude(pk=exclude_booking_id)
    return list(qs.values_list("effective_start_time", "effective_end_time"))


def _exception_intervals(tenant, host_id, window_start, window_end):
    """Time-off/leave intervals for a host within a window (blocks availability)."""
    qs = AvailabilityException.objects.filter(
        tenant=tenant,
        user_id=host_id,
        is_deleted=False,
        start_time__lt=window_end,
        end_time__gt=window_start,
    )
    return list(qs.values_list("start_time", "end_time"))


def _external_busy_intervals(tenant, host_id, window_start, window_end):
    """External-calendar busy intervals for a host, fetched on demand and cached
    briefly. Returns [] when the host has no CONNECTED integration or on any
    provider error (availability degrades gracefully rather than failing)."""
    from core.models.calendar_integration import CalendarIntegration

    integration = CalendarIntegration.objects.filter(
        tenant=tenant,
        user_id=host_id,
        is_deleted=False,
        sync_status=CalendarIntegration.SyncStatus.CONNECTED,
    ).first()
    if integration is None:
        return []

    # While a watch channel is live, webhook sync keeps ExternalBusyBlock fresh —
    # read it locally instead of a live free/busy call.
    if (
        integration.watch_channel_id
        and integration.watch_expires_at
        and integration.watch_expires_at > timezone.now()
    ):
        from core.models.external_busy_block import ExternalBusyBlock

        rows = ExternalBusyBlock.objects.filter(
            integration=integration,
            start_time__lt=window_end,
            end_time__gt=window_start,
        ).values_list("start_time", "end_time")
        return list(rows)

    cache_key = (
        f"freebusy:{integration.id}:{int(window_start.timestamp())}:{int(window_end.timestamp())}"
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        from core.services import calendars

        provider = calendars.get_provider_for(integration)
        intervals = [
            (b.start, b.end)
            for b in provider.get_busy(integration, window_start, window_end)
        ]
    except Exception as exc:  # noqa: BLE001 — never let calendar issues block booking
        logger.warning("free/busy fetch failed for integration %s: %s", integration.id, exc)
        return []

    cache.set(cache_key, intervals, getattr(settings, "EXTERNAL_FREEBUSY_CACHE_SECONDS", 60))
    return intervals


def host_on_leave(tenant, host_id, start, end):
    """True if the host has any AvailabilityException overlapping [start, end)."""
    return AvailabilityException.objects.filter(
        tenant=tenant,
        user_id=host_id,
        is_deleted=False,
        start_time__lt=end,
        end_time__gt=start,
    ).exists()


def _overlaps(intervals, eff_start, eff_end):
    return any(bs < eff_end and be > eff_start for bs, be in intervals)


def host_has_conflict(tenant, host_id, eff_start, eff_end, exclude_booking_id=None):
    """Authoritative-adjacent soft check: does the host already hold this range?"""
    qs = Booking.objects.filter(
        tenant=tenant,
        primary_host_id=host_id,
        is_deleted=False,
        status__in=ACTIVE_BOOKING_STATUSES,
        effective_start_time__lt=eff_end,
        effective_end_time__gt=eff_start,
    )
    if exclude_booking_id:
        qs = qs.exclude(pk=exclude_booking_id)
    return qs.exists()


def eligible_free_hosts(
    tenant, event_template, start_utc, end_utc, target_host_id=None, exclude_booking_id=None
):
    """
    Hosts that (a) can host this event type, (b) have this time inside their
    working hours, and (c) have no overlapping active booking. Ordering is by
    host id for determinism.

    With ``target_host_id`` this is a one-host yes/no check — an empty result
    means that host is either not skilled for the template or not free.
    """
    eff_start = start_utc - timedelta(minutes=event_template.buffer_before_minutes)
    eff_end = end_utc + timedelta(minutes=event_template.buffer_after_minutes)

    host_ids = _resolve_host_ids(tenant, event_template, target_host_id)
    profiles = _load_profiles(tenant, host_ids)

    free = []
    for host_id in host_ids:
        entry = profiles.get(host_id)
        if not entry:
            continue
        profile, rules = entry
        if not _within_working_hours(profile, rules, start_utc, end_utc):
            continue
        if host_on_leave(tenant, host_id, start_utc, end_utc):
            continue
        if _overlaps(
            _external_busy_intervals(tenant, host_id, start_utc, end_utc),
            start_utc,
            end_utc,
        ):
            continue
        if host_has_conflict(tenant, host_id, eff_start, eff_end, exclude_booking_id):
            continue
        free.append(host_id)
    return sorted(free, key=str)


def get_available_slots(
    tenant, event_template, date_from, date_to, target_host_id=None, now=None
):
    """
    Bookable slots between two dates (inclusive). One busy-interval query per
    host (no N+1); interval math is done in memory.

    - ``date_from`` / ``date_to`` are ``date`` objects.
    - Applies min-notice and horizon policy.
    - DIRECT: a slot is offered if >=1 considered host is free (the free hosts are
      carried on the slot). Pass ``target_host_id`` to scope this to the host who
      will actually be booked. COLLECTIVE: only if every skilled host is free.
    """
    now = now or timezone.now()
    min_start = now + timedelta(minutes=event_template.min_notice_minutes)
    horizon = now + timedelta(days=event_template.max_horizon_days)
    duration = timedelta(minutes=event_template.duration_minutes)
    step = timedelta(minutes=event_template.slot_granularity_minutes)
    buf_before = timedelta(minutes=event_template.buffer_before_minutes)
    buf_after = timedelta(minutes=event_template.buffer_after_minutes)

    host_ids = _resolve_host_ids(tenant, event_template, target_host_id)
    profiles = _load_profiles(tenant, host_ids)

    # Generous UTC window covering the local date range (+/- 1 day for tz skew).
    window_start = datetime.combine(date_from, time.min, tzinfo=UTC) - timedelta(days=1)
    window_end = datetime.combine(date_to, time.max, tzinfo=UTC) + timedelta(days=1)

    slot_map = {}  # start_utc -> set(host_id)
    considered_hosts = [h for h in host_ids if h in profiles]

    for host_id in considered_hosts:
        profile, rules = profiles[host_id]
        tz = profile.timezone.zone
        busy = (
            _busy_intervals(tenant, host_id, window_start, window_end)
            + _exception_intervals(tenant, host_id, window_start, window_end)
            + _external_busy_intervals(tenant, host_id, window_start, window_end)
        )

        day = date_from
        while day <= date_to:
            for rule in rules:
                if rule.weekday != day.weekday():
                    continue
                day_end_local = datetime.combine(day, rule.end_time, tzinfo=tz)
                cursor = datetime.combine(day, rule.start_time, tzinfo=tz)
                while cursor + duration <= day_end_local:
                    start_utc = cursor.astimezone(UTC)
                    end_utc = (cursor + duration).astimezone(UTC)
                    if min_start <= start_utc <= horizon:
                        eff_start = start_utc - buf_before
                        eff_end = end_utc + buf_after
                        if not _overlaps(busy, eff_start, eff_end):
                            slot_map.setdefault(start_utc, set()).add(host_id)
                    cursor += step
            day += timedelta(days=1)

    is_collective = (
        event_template.assignment_strategy == event_template.AssignmentStrategy.COLLECTIVE
    )
    required = len(considered_hosts)

    slots = []
    for start_utc in sorted(slot_map):
        hosts = slot_map[start_utc]
        if is_collective and len(hosts) != required:
            continue
        slots.append(
            Slot(
                start=start_utc,
                end=start_utc + duration,
                eligible_host_ids=tuple(sorted(hosts, key=str)),
            )
        )
    return slots
