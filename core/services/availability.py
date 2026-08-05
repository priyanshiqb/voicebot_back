"""
Availability engine.

Computes bookable slots and free hosts for an EventTemplate. All queries are
explicitly tenant-scoped (this code runs outside the DRF request layer, which
provides no automatic tenant filtering).

Every interval this module compares is an absolute UTC instant. Working hours
arrive pre-converted as AvailabilityWindow rows — the wall-clock -> UTC step,
including DST resolution and the overnight wrap, happens once at materialization
time in availability_materializer, not per request in here.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from core.models.availability_exception import AvailabilityException
from core.models.availability_profile import AvailabilityProfile
from core.models.availability_window import AvailabilityWindow
from core.models.booking import ACTIVE_BOOKING_STATUSES, Booking
from core.models.user_event_skill import UserEventSkill
from core.services.exceptions import ExternalCalendarUnavailable

logger = logging.getLogger(__name__)

UTC = ZoneInfo("UTC")


@dataclass(frozen=True)
class Slot:
    start: datetime  # UTC-aware
    end: datetime  # UTC-aware
    eligible_host_ids: tuple  # host ids free for this slot
    # True when at least one eligible host's external calendar could not be read,
    # so this slot is offered on unverified information. Booking it will be
    # refused while the provider is still unreachable (see eligible_free_hosts).
    external_check_failed: bool = False


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


def display_zone(tenant, target_host_id=None, requested=None):
    """The zone slot times should be *rendered* in, as ``(ZoneInfo, iana_name)``.

    Purely presentational — nothing here affects which slots are returned. The
    engine's answer is a set of UTC instants either way; this only decides how
    they are spelled for a human.

    Resolution order, most specific first:

      1. ``requested`` — the caller stated a zone, so it knows best. This is the
         viewer's zone, which is the only one that makes "9 AM" mean anything.
      2. The named host's own profile zone. Useful even when the viewer differs:
         it lets a UI say "6:30 PM for Dr. Sharma".
      3. The tenant's base zone.
      4. UTC.

    A malformed ``requested`` value falls through rather than raising — the
    serializer validates it, and a rendering hint must never be able to fail a
    query that would otherwise succeed.
    """
    if requested:
        try:
            return ZoneInfo(requested), requested
        except Exception:  # noqa: BLE001 — presentation only, never fatal
            logger.warning("ignoring unusable display timezone %r", requested)

    if target_host_id:
        profile = (
            AvailabilityProfile.objects.filter(
                tenant=tenant, user_id=target_host_id, is_deleted=False
            )
            .select_related("timezone")
            .first()
        )
        if profile and profile.timezone_id:
            return profile.timezone.zone, profile.timezone.timezone

    tz_model = getattr(tenant, "timezone", None)
    if tz_model is not None:
        return tz_model.zone, tz_model.timezone
    return UTC, "UTC"


def _as_utc(value, label):
    """Coerce a slot-range bound to an aware UTC datetime.

    A naive datetime compared against the aware instants in AvailabilityWindow
    raises ``TypeError: can't compare offset-naive and offset-aware datetimes``,
    which surfaces as a 500 rather than a 400. Four entry points reach this
    module with different parsing in front of them, so normalize here rather than
    trusting each of them.

    A ``date`` is rejected outright: silently reading it as UTC midnight is the
    exact ambiguity this signature exists to remove, and a caller passing one is
    a caller that has not yet decided whose day it means.
    """
    if isinstance(value, datetime):
        if timezone.is_naive(value):
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    raise TypeError(
        f"{label} must be an aware UTC datetime, not {type(value).__name__}. "
        f"Resolve the caller's local day to instants before calling."
    )


def _load_windows(tenant, host_ids, range_start, range_end):
    """``{host_id: [(start_utc, end_utc), ...]}`` overlapping the range, ascending.

    Pure interval overlap. A window is anchored on the local date its block
    STARTS, so an overnight block spills into the next day — but because this
    filter asks "does the interval intersect the range" rather than "what date is
    it", that block is found with no date fudging.
    """
    rows = (
        AvailabilityWindow.objects.filter(
            tenant=tenant,
            user_id__in=host_ids,
            start_utc__lt=range_end,
            end_utc__gt=range_start,
        )
        .order_by("user_id", "start_utc")
        .values_list("user_id", "start_utc", "end_utc")
    )
    by_host = {}
    for user_id, start, end in rows:
        by_host.setdefault(user_id, []).append((start, end))
    return by_host


def _merge(intervals):
    """Coalesce touching/overlapping intervals (input must be sorted by start).

    Needed so a booking may straddle two adjacent blocks: consecutive 24h days,
    or a schedule an author chose to split (Mon 22:00-23:59 + Tue 00:00-06:00)
    rather than express as one wrapping rule.
    """
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _warn_if_unmaterialized(tenant, host_id, rules, now=None):
    """Log ERROR if a host has rules but no future windows AT ALL.

    A host with rules but no windows *in the queried range* is completely normal
    — they just do not work then. The real failure is a host with rules and no
    materialized future whatsoever, which means the beat task died or the horizon
    ran out; that is what this checks, with one EXISTS query, and only on the
    already-empty path.

    There is deliberately no rule-walking fallback: a second code path here would
    hide exactly the failure this logs. Alert on this line.
    """
    if not rules:
        return
    now = now or timezone.now()
    if AvailabilityWindow.objects.filter(
        tenant=tenant, user_id=host_id, end_utc__gt=now
    ).exists():
        return
    logger.error(
        "host %s has %d availability rules but no materialized windows after %s "
        "— materialization is stale or failed",
        host_id,
        len(rules),
        now,
    )


def _within_working_windows(windows, start_utc, end_utc):
    """True if [start, end] fits inside one merged working window.

    No local-date logic and no same-day restriction: a booking that straddles
    midnight is now ordinary.
    """
    return any(s <= start_utc and end_utc <= e for s, e in _merge(windows))


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


def _external_busy(tenant, host_id, window_start, window_end):
    """External-calendar busy intervals for a host: ``(intervals, degraded)``.

    ``degraded`` is True only when the provider could not be reached — NOT when
    the host simply has no integration. That distinction is the whole point: an
    empty list from a healthy lookup means "free", while an empty list from a
    failed one means "unknown", and conflating them is how an outage silently
    advertises slots the host is actually busy for.

    Callers decide what to do with it: browsing degrades open and flags the slot,
    committing refuses (see eligible_free_hosts).
    """
    from core.models.calendar_integration import CalendarIntegration

    integration = CalendarIntegration.objects.filter(
        tenant=tenant,
        user_id=host_id,
        is_deleted=False,
        sync_status=CalendarIntegration.SyncStatus.CONNECTED,
    ).first()
    if integration is None:
        return [], False

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
        return list(rows), False

    cache_key = (
        f"freebusy:{integration.id}:{int(window_start.timestamp())}:{int(window_end.timestamp())}"
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return cached, False

    try:
        from core.services import calendars

        provider = calendars.get_provider_for(integration)
        intervals = [
            (b.start, b.end)
            for b in provider.get_busy(integration, window_start, window_end)
        ]
    except Exception as exc:  # noqa: BLE001 — a provider outage must not raise here
        # ERROR, not WARNING: this is the state in which the system can advertise
        # a slot the host is actually busy for, and nothing else surfaces it.
        logger.error(
            "free/busy fetch failed for integration %s (%s) — availability for "
            "host %s is unverified",
            integration.id,
            exc,
            host_id,
            exc_info=True,
        )
        CalendarIntegration.objects.filter(pk=integration.pk).update(
            last_error=str(exc)[:500]
        )
        return [], True

    cache.set(cache_key, intervals, getattr(settings, "EXTERNAL_FREEBUSY_CACHE_SECONDS", 60))
    return intervals, False


def _external_busy_intervals(tenant, host_id, window_start, window_end):
    """Intervals only, discarding the degraded flag. For callers that have already
    decided degradation is acceptable."""
    intervals, _degraded = _external_busy(tenant, host_id, window_start, window_end)
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
    windows = _load_windows(tenant, host_ids, start_utc, end_utc)

    free = []
    for host_id in host_ids:
        entry = profiles.get(host_id)
        if not entry:
            continue
        _profile, rules = entry
        host_windows = windows.get(host_id, [])
        if not host_windows:
            _warn_if_unmaterialized(tenant, host_id, rules)
        # Working hours are checked on the RAW times, matching slot generation:
        # buffers pad the surrounding calendar, they do not extend the shift.
        if not _within_working_windows(host_windows, start_utc, end_utc):
            continue
        # Everything that makes a host BUSY is checked buffer-inclusive, which is
        # what get_available_slots does. These used to disagree: leave and external
        # busy were tested against the raw times here, so a leave overlapping only
        # a booking's buffer hid the slot at listing time and then passed the
        # create-time check.
        if host_on_leave(tenant, host_id, eff_start, eff_end):
            continue
        external, degraded = _external_busy(tenant, host_id, eff_start, eff_end)
        if degraded:
            # Commit path: refuse rather than book against a calendar we could not
            # read. An unverified booking double-books the host with an external
            # meeting, and nothing here can detect that afterwards.
            raise ExternalCalendarUnavailable(
                "Could not verify the host's calendar availability. Please try again."
            )
        if _overlaps(external, eff_start, eff_end):
            continue
        if host_has_conflict(tenant, host_id, eff_start, eff_end, exclude_booking_id):
            continue
        free.append(host_id)
    return sorted(free, key=str)


def get_available_slots(
    tenant, event_template, range_start, range_end, target_host_id=None, now=None
):
    """
    Bookable slots in the half-open UTC interval ``[range_start, range_end)``.
    One busy-interval query per host (no N+1); interval math is done in memory.

    ``range_start`` / ``range_end`` are **aware UTC datetimes**, not dates. A date
    is not an instant: "August 8th" is a different 24 hours in every timezone, so
    a date range forces the server to guess whose day the caller meant. The
    caller knows — it is the only component that knows which timezone the human
    picked — so it resolves the day to instants and this function does pure UTC
    interval math.

    - Applies min-notice and horizon policy.
    - DIRECT: a slot is offered if >=1 considered host is free (the free hosts are
      carried on the slot). Pass ``target_host_id`` to scope this to the host who
      will actually be booked. COLLECTIVE: only if every skilled host is free.
    """
    now = now or timezone.now()
    range_start = _as_utc(range_start, "range_start")
    range_end = _as_utc(range_end, "range_end")
    min_start = now + timedelta(minutes=event_template.min_notice_minutes)
    horizon = now + timedelta(days=event_template.max_horizon_days)
    duration = timedelta(minutes=event_template.duration_minutes)
    step = timedelta(minutes=event_template.slot_granularity_minutes)
    buf_before = timedelta(minutes=event_template.buffer_before_minutes)
    buf_after = timedelta(minutes=event_template.buffer_after_minutes)

    host_ids = _resolve_host_ids(tenant, event_template, target_host_id)
    profiles = _load_profiles(tenant, host_ids)

    # A window overlapping the range can start before it (an overnight block the
    # range begins partway through), so widen the LOAD by a day. Slots are still
    # bounded by the requested range itself, below.
    window_start = range_start - timedelta(days=1)
    window_end = range_end + timedelta(days=1)
    windows = _load_windows(tenant, host_ids, window_start, window_end)

    slot_map = {}  # start_utc -> set(host_id)
    considered_hosts = [h for h in host_ids if h in profiles]
    # Hosts whose external calendar could not be read. Browsing still offers their
    # slots — a provider outage should not empty the calendar — but every slot they
    # appear in is flagged so the caller knows it is unverified.
    unverified_hosts = set()

    for host_id in considered_hosts:
        _profile, rules = profiles[host_id]
        host_windows = windows.get(host_id, [])
        if not host_windows:
            _warn_if_unmaterialized(tenant, host_id, rules, now=now)
            continue

        external, degraded = _external_busy(tenant, host_id, window_start, window_end)
        if degraded:
            unverified_hosts.add(host_id)
        busy = (
            _busy_intervals(tenant, host_id, window_start, window_end)
            + _exception_intervals(tenant, host_id, window_start, window_end)
            + external
        )

        # Raw windows, not merged, deliberately: the cursor restarts at each
        # window's own start, which is the same instant the old per-day rule loop
        # started from — so slot alignment is preserved exactly. Merging here
        # would let a granularity that does not divide the day (e.g. 25 min)
        # drift across day boundaries and silently move every advertised time.
        for w_start, w_end in host_windows:
            cursor = w_start
            while cursor + duration <= w_end:
                start_utc, end_utc = cursor, cursor + duration
                # Half-open [range_start, range_end): windows were loaded a day
                # wider, so the requested range is enforced here. Half-open means
                # consecutive ranges tile without overlapping or dropping a slot.
                if (
                    range_start <= start_utc < range_end
                    and min_start <= start_utc <= horizon
                ):
                    if not _overlaps(busy, start_utc - buf_before, end_utc + buf_after):
                        slot_map.setdefault(start_utc, set()).add(host_id)
                cursor += step

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
                external_check_failed=bool(hosts & unverified_hosts),
            )
        )
    return slots
