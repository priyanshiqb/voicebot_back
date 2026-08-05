"""Shared fixtures/helpers for scheduling tests."""

import secrets
from datetime import datetime, time
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


def make_host(
    tenant,
    template,
    tz=None,
    email=None,
    weekdays=range(7),
    start=time(0, 0),
    end=time(23, 59),
):
    """A user skilled for `template` with an availability profile. Defaults to
    24/7 availability so booking times are independent of wall-clock time."""
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
