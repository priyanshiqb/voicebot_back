"""
Materializes wall-clock AvailabilityRule rows into absolute UTC AvailabilityWindow
rows. This is the ONLY place the wall-clock -> UTC conversion happens for
scheduling, and it holds no timezone logic of its own: every conversion goes
through ``onboard.Timezone.to_utc()``.

Windows are pure derived data. They are hard-deleted and rebuilt, never soft-
deleted, and every function here is idempotent — re-running always converges on
the current rules.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone as dj_tz

from core.models.availability_profile import AvailabilityProfile
from core.models.availability_window import AvailabilityWindow
from core.models.event_template import MAX_HORIZON_DAYS, EventTemplate
from utils.querysets import batched_queryset

logger = logging.getLogger(__name__)

DEFAULT_MATERIALIZE_DAYS = 90


def rule_window(rule, timezone_obj, local_day):
    """
    The absolute UTC interval for one rule on one local date.

    ``local_day`` is the day the block STARTS on. An overnight rule
    (``end_time <= start_time``) resolves its end against the FOLLOWING local
    date — that is the whole point of this function.

    Returns ``(start_utc, end_utc, dst_flag)``. The caller must reject
    ``end_utc <= start_utc``: that means the block fell wholly inside a
    spring-forward gap and does not occur on this date.
    """
    start_utc, start_flag = timezone_obj.to_utc(local_day, rule.start_time, prefer_fold=0)

    end_day = local_day + timedelta(days=1) if rule.wraps_midnight else local_day
    # prefer_fold=1 on the END so a block spanning a fall-back night covers the
    # repeated hour rather than stopping at its first occurrence.
    end_utc, end_flag = timezone_obj.to_utc(end_day, rule.end_time, prefer_fold=1)

    return start_utc, end_utc, (start_flag or end_flag)


def horizon_days_for(tenant):
    """
    How many days ahead to materialize for a tenant.

    ``AVAILABILITY_MATERIALIZE_DAYS`` is the floor, but a tenant whose event
    templates advertise a longer booking horizon needs windows that far out or
    the engine returns nothing for the tail of its own advertised range. Capped
    at MAX_HORIZON_DAYS, which is also the validator bound on the field.
    """
    base = int(getattr(settings, "AVAILABILITY_MATERIALIZE_DAYS", DEFAULT_MATERIALIZE_DAYS))
    longest = (
        EventTemplate.objects.filter(tenant=tenant, is_deleted=False)
        .order_by("-max_horizon_days")
        .values_list("max_horizon_days", flat=True)
        .first()
    ) or 0
    # +1 day of slack: the horizon is measured from `now`, but materialization is
    # anchored on local dates, so the last local day must be fully covered.
    return min(max(base, int(longest) + 1), MAX_HORIZON_DAYS)


def purge_profile(profile):
    """Drop every materialized window for a profile. Returns the number removed."""
    # all_objects is a plain Manager: BaseQuerySet.delete() raises by design, and
    # bulk delete is intentional here — windows are derived rows with no audit
    # value, exactly like ExternalBusyBlock.
    deleted, _ = AvailabilityWindow.all_objects.filter(profile=profile).delete()
    return deleted


def materialize_profile(profile, date_from, date_to):
    """
    Regenerate ``profile``'s windows for the inclusive local-date range.

    Idempotent: the range is purged and rebuilt. Returns the number of windows
    written. An inactive or soft-deleted profile is purged and contributes
    nothing — this is what makes ``is_active`` actually mean something for
    availability.
    """
    if profile.is_deleted or not profile.is_active:
        purge_profile(profile)
        return 0

    tz_obj = profile.timezone  # the Timezone model, not a ZoneInfo
    rules = list(profile.rules.all())

    rows, day = [], date_from
    while day <= date_to:
        for rule in rules:
            if rule.weekday != day.weekday():
                continue
            start_utc, end_utc, flag = rule_window(rule, tz_obj, day)
            if end_utc <= start_utc:
                # The block is wholly inside a spring-forward gap, so it does not
                # occur on this date. Expected, not an error — and it must be
                # skipped HERE, or the end_utc > start_utc check constraint aborts
                # the whole profile's materialization.
                logger.info(
                    "skipping DST-gap window rule=%s day=%s tz=%s",
                    rule.id,
                    day,
                    tz_obj.timezone,
                )
                continue
            rows.append(
                AvailabilityWindow(
                    tenant=profile.tenant,
                    profile=profile,
                    rule=rule,
                    user_id=profile.user_id,
                    local_date=day,
                    start_utc=start_utc,
                    end_utc=end_utc,
                    dst_flag=flag,
                )
            )
        day += timedelta(days=1)

    with transaction.atomic():
        # MUST lock the profile first. Without it two concurrent materializations
        # of the same profile+range race under READ COMMITTED (this project's
        # default — no isolation level is configured):
        #     T1 DELETE range ... T2 DELETE range (blocks on T1's row locks)
        #     T1 INSERT, COMMIT  ... T2 wakes, re-evaluates, deletes 0 rows,
        #     T2 INSERT -> IntegrityError on unique(rule, local_date)
        # Reachable whenever the write-path task overlaps the daily beat task, or
        # on two rapid PATCHes of the same profile.
        AvailabilityProfile.objects.select_for_update().get(pk=profile.pk)

        AvailabilityWindow.all_objects.filter(
            profile=profile, local_date__gte=date_from, local_date__lte=date_to
        ).delete()
        AvailabilityWindow.objects.bulk_create(rows, batch_size=1000)
    return len(rows)


def materialize_profile_from_today(profile, days=None):
    """Materialize a profile over the standard forward horizon."""
    days = days or horizon_days_for(profile.tenant)
    # Start one day back: an overnight rule anchored yesterday can still be
    # running now, and the engine finds it by interval overlap.
    today = dj_tz.now().astimezone(profile.timezone.zone).date()
    return materialize_profile(profile, today - timedelta(days=1), today + timedelta(days=days))


def materialize_tenant(tenant, days=None):
    """Materialize every active profile in a tenant. Returns (profiles, windows)."""
    days = days or horizon_days_for(tenant)
    profiles = (
        AvailabilityProfile.objects.filter(tenant=tenant, is_deleted=False)
        .select_related("timezone", "tenant")
        .prefetch_related("rules")
    )
    seen = written = 0
    for profile in batched_queryset(profiles, batch_size=200):
        seen += 1
        try:
            written += materialize_profile_from_today(profile, days=days)
        except Exception:  # noqa: BLE001
            # One malformed profile must not strand every later one, and the next
            # run would hit the same row again.
            logger.exception("materialization failed for profile %s", profile.id)
    return seen, written


def purge_past_windows(before=None):
    """
    Drop windows that ended over a day ago.

    Every engine query is bounded below by ``now``, so these can never be read —
    they are just what makes the table grow without bound.
    """
    before = before or (dj_tz.now() - timedelta(days=1))
    deleted, _ = AvailabilityWindow.all_objects.filter(end_utc__lt=before).delete()
    return deleted
