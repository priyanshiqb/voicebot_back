"""
Materializer mechanics: idempotency, the concurrency guard, horizon sizing,
purge, and the is_active semantics materialization now enforces.

Availability became eventually-consistent with a horizon, which buys a new
failure mode — stale windows silently narrow a host's availability instead of
erroring. The last class here covers the canary that makes that visible.
"""

import threading
from datetime import date, time, timedelta
from unittest import skipUnless

from django.db import connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone as dj_tz

from core.models.availability_profile import AvailabilityProfile
from core.models.availability_rule import AvailabilityRule
from core.models.availability_window import AvailabilityWindow
from core.models.event_template import MAX_HORIZON_DAYS
from core.models.user_event_skill import UserEventSkill
from core.services import availability, availability_materializer as materializer
from core.tests.scheduling import factories as f

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)

MONDAY = date(2027, 3, 15)


def _profile_with_rule(tenant, tz, weekday=0, start=time(9, 0), end=time(17, 0)):
    user = f.make_user(tenant)
    profile = AvailabilityProfile.objects.create(tenant=tenant, user=user, timezone=tz)
    AvailabilityRule.objects.create(
        tenant=tenant, profile=profile, weekday=weekday, start_time=start, end_time=end
    )
    return profile


@REQUIRES_PG
class IdempotencyTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.tz = f.make_timezone()
        self.profile = _profile_with_rule(self.tenant, self.tz)
        self.range = (MONDAY, MONDAY + timedelta(days=13))  # two Mondays

    def test_rerunning_converges_instead_of_duplicating(self):
        first = materializer.materialize_profile(self.profile, *self.range)
        second = materializer.materialize_profile(self.profile, *self.range)

        self.assertEqual(first, 2)
        self.assertEqual(second, 2)
        self.assertEqual(AvailabilityWindow.objects.filter(profile=self.profile).count(), 2)

    def test_edited_rule_replaces_the_old_windows(self):
        materializer.materialize_profile(self.profile, *self.range)
        AvailabilityRule.objects.filter(profile=self.profile).update(start_time=time(11, 0))
        materializer.materialize_profile(self.profile, *self.range)

        hours = set(
            AvailabilityWindow.objects.filter(profile=self.profile).values_list(
                "start_utc__hour", flat=True
            )
        )
        self.assertEqual(hours, {11}, "stale 09:00 windows survived the edit")

    def test_rematerializing_one_range_leaves_other_dates_alone(self):
        materializer.materialize_profile(self.profile, *self.range)
        # Rebuild only the first week.
        materializer.materialize_profile(self.profile, MONDAY, MONDAY + timedelta(days=6))

        self.assertEqual(AvailabilityWindow.objects.filter(profile=self.profile).count(), 2)

    def test_deleted_rule_leaves_no_orphan_windows(self):
        materializer.materialize_profile(self.profile, *self.range)
        # all_objects: BaseQuerySet.delete() raises by design. This is the same
        # hard delete the profile serializer does per-instance when rules change.
        AvailabilityRule.all_objects.filter(profile=self.profile).delete()
        materializer.materialize_profile(self.profile, *self.range)

        self.assertEqual(AvailabilityWindow.objects.filter(profile=self.profile).count(), 0)


@REQUIRES_PG
class ProfileStateTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.tz = f.make_timezone()
        self.profile = _profile_with_rule(self.tenant, self.tz)

    def test_inactive_profile_is_purged_not_materialized(self):
        """is_active was previously honoured inconsistently — the base manager
        filters is_deleted but NOT is_active, so an 'off' profile still produced
        slots. Materialization is where that now actually takes effect."""
        materializer.materialize_profile(self.profile, MONDAY, MONDAY)
        self.assertEqual(AvailabilityWindow.objects.filter(profile=self.profile).count(), 1)

        self.profile.is_active = False
        self.profile.save(update_fields=["is_active"])
        written = materializer.materialize_profile(self.profile, MONDAY, MONDAY)

        self.assertEqual(written, 0)
        self.assertEqual(AvailabilityWindow.objects.filter(profile=self.profile).count(), 0)

    def test_soft_deleted_profile_is_purged(self):
        materializer.materialize_profile(self.profile, MONDAY, MONDAY)
        self.profile.soft_delete()

        written = materializer.materialize_profile(self.profile, MONDAY, MONDAY)
        self.assertEqual(written, 0)
        self.assertEqual(AvailabilityWindow.objects.filter(profile=self.profile).count(), 0)

    def test_purge_removes_every_date(self):
        materializer.materialize_profile(self.profile, MONDAY, MONDAY + timedelta(days=27))
        self.assertEqual(materializer.purge_profile(self.profile), 4)
        self.assertEqual(AvailabilityWindow.objects.filter(profile=self.profile).count(), 0)

    def test_purge_past_windows_keeps_the_future(self):
        past = dj_tz.now().date() - timedelta(days=30)
        materializer.materialize_profile(self.profile, past, past + timedelta(days=60))
        before = AvailabilityWindow.objects.filter(profile=self.profile).count()

        materializer.purge_past_windows()
        after = AvailabilityWindow.objects.filter(profile=self.profile).count()

        self.assertLess(after, before)
        self.assertGreater(after, 0)
        self.assertFalse(
            AvailabilityWindow.objects.filter(
                profile=self.profile, end_utc__lt=dj_tz.now() - timedelta(days=1)
            ).exists()
        )


@REQUIRES_PG
class HorizonTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()

    @override_settings(AVAILABILITY_MATERIALIZE_DAYS=90)
    def test_default_horizon_is_the_setting(self):
        f.make_template(self.tenant, max_horizon_days=30)
        self.assertEqual(materializer.horizon_days_for(self.tenant), 90)

    @override_settings(AVAILABILITY_MATERIALIZE_DAYS=90)
    def test_a_longer_advertised_horizon_wins(self):
        """Otherwise the engine returns nothing for the tail of a range the
        template itself advertises as bookable."""
        f.make_template(self.tenant, max_horizon_days=180)
        self.assertEqual(materializer.horizon_days_for(self.tenant), 181)

    @override_settings(AVAILABILITY_MATERIALIZE_DAYS=90)
    def test_horizon_is_hard_capped(self):
        """max_horizon_days is validated to <= MAX_HORIZON_DAYS, but the cap is
        restated here: the field is a PositiveSmallIntegerField, so a row written
        around the validator (fixture, shell, bad migration) could otherwise ask
        for ~90 years of windows per profile."""
        f.make_template(self.tenant, max_horizon_days=MAX_HORIZON_DAYS)
        self.assertEqual(materializer.horizon_days_for(self.tenant), MAX_HORIZON_DAYS)

    @override_settings(AVAILABILITY_MATERIALIZE_DAYS=90)
    def test_tenant_with_no_templates_uses_the_setting(self):
        self.assertEqual(materializer.horizon_days_for(self.tenant), 90)


@REQUIRES_PG
class TenantMaterializationTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.tz = f.make_timezone()
        self.template = f.make_template(self.tenant)

    def test_one_broken_profile_does_not_strand_the_others(self):
        """Same isolation rule as the beat loops: a bad row must not abort the run
        and then poison every subsequent one."""
        good_a = _profile_with_rule(self.tenant, self.tz)
        broken = _profile_with_rule(self.tenant, self.tz)
        good_b = _profile_with_rule(self.tenant, self.tz)

        real = materializer.materialize_profile

        def flaky(profile, *args, **kwargs):
            if profile.pk == broken.pk:
                raise RuntimeError("boom")
            return real(profile, *args, **kwargs)

        with self.settings(AVAILABILITY_MATERIALIZE_DAYS=14):
            with self.assertLogs("core.services.availability_materializer", level="ERROR"):
                original = materializer.materialize_profile
                materializer.materialize_profile = flaky
                try:
                    seen, written = materializer.materialize_tenant(self.tenant)
                finally:
                    materializer.materialize_profile = original

        self.assertEqual(seen, 3)
        self.assertGreater(written, 0)
        for profile in (good_a, good_b):
            self.assertTrue(
                AvailabilityWindow.objects.filter(profile=profile).exists(),
                "a sibling profile was stranded by the broken one",
            )
        self.assertFalse(AvailabilityWindow.objects.filter(profile=broken).exists())


@REQUIRES_PG
class StaleMaterializationTests(TestCase):
    """The canary for the failure mode materialization introduces."""

    def setUp(self):
        self.tenant = f.make_tenant()
        self.tz = f.make_timezone()
        self.template = f.make_template(self.tenant)
        self.user = f.make_user(self.tenant)
        UserEventSkill.objects.create(
            tenant=self.tenant, user=self.user, event_template=self.template
        )
        self.profile = AvailabilityProfile.objects.create(
            tenant=self.tenant, user=self.user, timezone=self.tz
        )
        for weekday in range(7):
            AvailabilityRule.objects.create(
                tenant=self.tenant,
                profile=self.profile,
                weekday=weekday,
                start_time=time(0, 0),
                end_time=time(0, 0),
            )

    def test_rules_without_windows_log_an_error(self):
        with self.assertLogs("core.services.availability", level="ERROR") as logs:
            slots = availability.get_available_slots(
                self.tenant, self.template, MONDAY, MONDAY, target_host_id=self.user.id
            )
        self.assertEqual(slots, [])
        self.assertTrue(any("materialization is stale" in line for line in logs.output))

    def test_not_working_that_day_is_silent(self):
        """A host who simply does not work on the queried date must NOT trip the
        alert — a canary that cries wolf on every ordinary day is worse than none."""
        AvailabilityRule.all_objects.filter(profile=self.profile).exclude(weekday=0).delete()
        f.materialize(self.profile)
        tuesday = dj_tz.now().date() + timedelta(days=(1 - dj_tz.now().weekday()) % 7 or 7)

        with self.assertNoLogs("core.services.availability", level="ERROR"):
            availability.get_available_slots(
                self.tenant, self.template, tuesday, tuesday, target_host_id=self.user.id
            )


@REQUIRES_PG
class ConcurrentMaterializationTests(TransactionTestCase):
    """Two writers, one profile.

    Under READ COMMITTED (this project's default) an unlocked delete-then-insert
    races: T2 blocks on T1's row locks, wakes after T1 commits, re-evaluates its
    DELETE against rows that no longer match, deletes nothing, and then collides
    on unique(rule, local_date). Reachable whenever the write-path task overlaps
    the daily beat task, or on two rapid PATCHes.
    """

    def setUp(self):
        self.tenant = f.make_tenant()
        self.tz = f.make_timezone()
        self.profile = _profile_with_rule(self.tenant, self.tz)

    def test_concurrent_materialize_serializes(self):
        errors = []
        barrier = threading.Barrier(2, timeout=10)

        def run():
            try:
                barrier.wait()
                profile = (
                    AvailabilityProfile.objects.select_related("timezone", "tenant")
                    .prefetch_related("rules")
                    .get(pk=self.profile.pk)
                )
                materializer.materialize_profile(
                    profile, MONDAY, MONDAY + timedelta(days=27)
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        self.assertEqual(errors, [], f"concurrent materialization raced: {errors}")
        self.assertEqual(AvailabilityWindow.objects.filter(profile=self.profile).count(), 4)
