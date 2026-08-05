"""Shared fixtures/helpers for scheduling tests."""

import secrets
from datetime import datetime, time, timedelta
from datetime import timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.cache import cache

from onboard.models.timezone import Timezone
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from core.models.availability_exception import AvailabilityException
from core.models.availability_profile import AvailabilityProfile
from core.models.availability_rule import AvailabilityRule
from core.models.event_template import EventTemplate
from core.models.user_event_skill import UserEventSkill

User = get_user_model()

UTC = dt_timezone.utc
# Fixed reference instant so time-of-day never makes tests flaky.
NOW = datetime(2027, 3, 15, 8, 0, tzinfo=UTC)
ATTENDEES = [{"name": "Customer", "email": "customer@example.com"}]

_counter = {"n": 0}


def _uid():
    _counter["n"] += 1
    return _counter["n"]


def at(hour, minute=0, day=16, month=3, year=2027):
    """A fixed UTC datetime (defaults to 2027-03-16, the day after NOW)."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def utc_day(day, days=1):
    """``(range_start, range_end)`` spanning `days` UTC days from `day`.

    `get_available_slots` takes instants, not dates — a date is a different 24
    hours in every zone, so the caller has to say which one it means. Tests that
    do not care about timezone edges mean "the UTC day", which is this.
    """
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + timedelta(days=days)


def local_day(day, tz, days=1):
    """``(range_start, range_end)`` spanning `days` LOCAL days in zone `tz`.

    This is what a frontend does: resolve the user's chosen day to instants using
    the timezone they are viewing in.
    """
    zone = tz.zone if hasattr(tz, "zone") else tz
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=days), time.min, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


def make_tenant(name=None, subdomain=None):
    n = _uid()
    return Tenant.objects.create(
        name=name or f"Tenant {n}", subdomain=subdomain or f"tenant-{n}"
    )


def make_user(tenant, email=None, password="password"):
    email = email or f"user{_uid()}@example.com"
    user = User.objects.create_user(email=email, username=email, password=password)
    TenantUserMapping.objects.create(user=user, tenant=tenant)
    return user


def make_timezone(name="UTC", offset="UTC+00:00", region="UTC"):
    tz, _ = Timezone.objects.get_or_create(
        timezone=name, defaults={"offset": offset, "region": region}
    )
    return tz


def make_template(tenant, **kwargs):
    defaults = dict(
        name=f"Event {_uid()}",
        duration_minutes=30,
        slot_granularity_minutes=30,
        min_notice_minutes=0,
        max_horizon_days=365,
        cancellation_lead_minutes=60,
        reschedule_lead_minutes=60,
        assignment_strategy=EventTemplate.AssignmentStrategy.DIRECT,
    )
    defaults.update(kwargs)
    return EventTemplate.objects.create(tenant=tenant, **defaults)


def materialize(profile, date_from=None, date_to=None):
    """Rebuild a profile's UTC availability windows.

    The engine reads AvailabilityWindow, never AvailabilityRule, so a profile
    built directly through the ORM is INVISIBLE to it until this runs.

    With no explicit range this covers both time anchors the suite uses: real
    ``now`` (tests calling ``timezone.now()``) and the fixed ``NOW`` in 2027.
    One rule matches per local date, so a 7-rule profile costs one row per day,
    not seven.
    """
    from django.utils import timezone as dj_timezone

    from core.services import availability_materializer as materializer

    if date_from and date_to:
        return materializer.materialize_profile(profile, date_from, date_to)

    written = 0
    for anchor in {dj_timezone.now().date(), NOW.date()}:
        written += materializer.materialize_profile(
            profile, anchor - timedelta(days=2), anchor + timedelta(days=45)
        )
    return written


def make_host(
    tenant,
    template,
    tz=None,
    email=None,
    weekdays=range(7),
    start=time(0, 0),
    end=time(0, 0),
):
    """A user skilled for `template` with a materialized availability profile.

    Defaults to true 24/7 (``00:00 -> 00:00`` is a full day, and seven of them
    are contiguous) so booking times are independent of wall-clock time.
    """
    tz = tz or make_timezone()
    user = make_user(tenant, email=email)
    UserEventSkill.objects.create(tenant=tenant, user=user, event_template=template)
    profile = AvailabilityProfile.objects.create(tenant=tenant, user=user, timezone=tz)
    for weekday in weekdays:
        AvailabilityRule.objects.create(
            tenant=tenant,
            profile=profile,
            weekday=weekday,
            start_time=start,
            end_time=end,
        )
    materialize(profile)
    return user


def oauth_state(user, tenant, provider="GOOGLE"):
    """A signed OAuth `state` with its single-use nonce primed in the cache.

    The calendar callback burns the nonce (`cache.delete`) to stop a leaked state
    from being replayed, so a hand-rolled state without one is rejected with 400.
    Tests that hit the callback must go through here.
    """
    from core.views.calendar_integration import _STATE_SALT, _STATE_MAX_AGE

    nonce = secrets.token_hex(16)
    cache.set(f"oauth_state_{nonce}", True, timeout=_STATE_MAX_AGE)
    return signing.dumps(
        {
            "user_id": str(user.id),
            "tenant_id": str(tenant.id),
            "provider": provider,
            "nonce": nonce,
        },
        salt=_STATE_SALT,
    )


def make_exception(tenant, user, start, end, exc_type=None):
    return AvailabilityException.objects.create(
        tenant=tenant,
        user=user,
        start_time=start,
        end_time=end,
        type=exc_type or AvailabilityException.ExceptionType.EMERGENCY,
    )
