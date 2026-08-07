"""
A host's availability profile follows their timezone when they relocate.

The rule that needs the most care is the one about *not* syncing: a profile whose
timezone already differed from the user's was set that way deliberately — a host in
Kolkata keeping New York hours for US customers — and re-anchoring it would move
every slot they offer by nine and a half hours without anyone asking.
"""

from unittest import skipUnless

from django.db import connection
from django.test import TestCase

from core.models.availability_profile import AvailabilityProfile
from core.models.availability_window import AvailabilityWindow
from core.tests.scheduling import factories as f


@skipUnless(connection.vendor == "postgresql", "Scheduling tests require PostgreSQL.")
class ProfileTimezoneSyncTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.kolkata = f.make_timezone("Asia/Kolkata", "UTC+05:30", "Asia")
        self.london = f.make_timezone("Europe/London", "UTC+00:00", "Europe")
        self.new_york = f.make_timezone("America/New_York", "UTC-05:00", "America")

        self.user = f.make_user(self.tenant)
        self.user.timezone = self.kolkata
        self.user.save()

        self.profile = AvailabilityProfile.objects.create(
            tenant=self.tenant,
            user=self.user,
            timezone=self.kolkata,
            name="Working hours",
        )

    def _move_user_to(self, tz):
        self.user.timezone = tz
        self.user.save()
        self.profile.refresh_from_db()

    def test_the_profile_follows_the_user(self):
        self._move_user_to(self.london)
        self.assertEqual(self.profile.timezone_id, self.london.id)

    def test_a_profile_that_already_differed_is_left_alone(self):
        """The case that must not be clobbered: hours deliberately set to a zone
        the host does not live in."""
        self.profile.timezone = self.new_york
        self.profile.save(update_fields=["timezone"])

        self._move_user_to(self.london)

        self.assertEqual(self.profile.timezone_id, self.new_york.id)

    def test_a_user_with_no_previous_timezone_does_not_sync(self):
        """Nothing to have been in step with, so no evidence the profile's zone was
        ever meant to track theirs."""
        self.user.timezone = None
        self.user.save()
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.timezone_id, self.kolkata.id)

        self._move_user_to(self.london)

        self.assertEqual(self.profile.timezone_id, self.kolkata.id)

    def test_clearing_the_users_timezone_does_not_clear_the_profiles(self):
        """AvailabilityProfile.timezone is non-nullable — the rules would have
        nothing to resolve against."""
        self._move_user_to(None)
        self.assertEqual(self.profile.timezone_id, self.kolkata.id)

    def test_saving_a_user_without_changing_timezone_is_a_no_op(self):
        self.profile.timezone = self.new_york
        self.profile.save(update_fields=["timezone"])

        self.user.first_name = "Renamed"
        self.user.save()

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.timezone_id, self.new_york.id)

    def test_a_user_with_no_profile_is_harmless(self):
        other = f.make_user(self.tenant)
        other.timezone = self.kolkata
        other.save()
        other.timezone = self.london
        other.save()  # must not raise

        self.assertFalse(AvailabilityProfile.objects.filter(user=other).exists())

    def test_the_wall_clock_rules_are_not_rewritten(self):
        """Only the zone changes. '09:00' still reads '09:00' — it now means 09:00
        somewhere else, which is the entire point."""
        from core.models.availability_rule import AvailabilityRule

        AvailabilityRule.objects.create(
            tenant=self.tenant,
            profile=self.profile,
            weekday=0,
            start_time="09:00:00",
            end_time="17:00:00",
        )

        self._move_user_to(self.london)

        rule = AvailabilityRule.objects.get(profile=self.profile)
        self.assertEqual(str(rule.start_time), "09:00:00")
        self.assertEqual(str(rule.end_time), "17:00:00")

    def test_windows_are_rebuilt_against_the_new_zone(self):
        """The materialized UTC windows are the thing that actually decides when a
        host is bookable, so a timezone change that did not rebuild them would
        leave the profile claiming one thing and the engine offering another."""
        from core.models.availability_rule import AvailabilityRule
        from core.services import availability_materializer as materializer

        AvailabilityRule.objects.create(
            tenant=self.tenant,
            profile=self.profile,
            weekday=0,
            start_time="09:00:00",
            end_time="17:00:00",
        )
        materializer.materialize_profile_from_today(self.profile)
        before = set(
            AvailabilityWindow.all_objects.filter(profile=self.profile).values_list(
                "start_utc", flat=True
            )
        )
        self.assertTrue(before)

        with self.captureOnCommitCallbacks(execute=True):
            self._move_user_to(self.london)

        after = set(
            AvailabilityWindow.all_objects.filter(profile=self.profile).values_list(
                "start_utc", flat=True
            )
        )
        self.assertTrue(after)
        # 09:00 Kolkata and 09:00 London are different instants, so every window moved.
        self.assertFalse(before & after)
