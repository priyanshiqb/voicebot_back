"""
The availability-profile write path, through the real HTTP API.

Every other test in this package calls the materializer directly. That leaves the
actual production chain untested:

    POST /availability-profiles/  ->  serializer.create
      ->  transaction.on_commit   ->  materialize_profile_task.delay
      ->  AvailabilityWindow rows ->  slots the engine will actually offer

Each link is individually plausible and collectively easy to get wrong — an
on_commit hook registered inside a transaction that later rolls back, a task that
reads the profile through a manager filtering it out, a serializer that skips
regeneration on the one field that needed it. These tests exercise the whole
chain, so a break anywhere in it fails here.

Celery runs eagerly under test, but `.delay()` is still dispatched from the
post-commit hook, so the ordering being asserted is real.
"""

from datetime import timedelta
from unittest import skipUnless

from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from core.models.availability_profile import AvailabilityProfile
from core.models.availability_window import AvailabilityWindow
from core.models.user_event_skill import UserEventSkill
from core.tests.scheduling import factories as f

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)

PROFILES_URL = "/api/v1/core/availability-profiles/"
SLOTS_URL = "/api/v1/core/availability/available-slots/"
BOOKINGS_URL = "/api/v1/core/bookings/"


@REQUIRES_PG
class AvailabilityProfileWritePathTests(APITestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.actor = f.make_user(self.tenant)
        self.tz = f.make_timezone()
        self.client = APIClient()
        self.client.force_authenticate(user=self.actor)

    def _payload(self, user, rules, **extra):
        body = {
            "user": str(user.id),
            "timezone": str(self.tz.id),
            "name": "Default",
            "rules": rules,
        }
        body.update(extra)
        return body

    def _create(self, user, rules, **extra):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                PROFILES_URL, self._payload(user, rules, **extra), format="json"
            )
        return response

    # --- create ---------------------------------------------------------------

    def test_create_materializes_windows(self):
        """The core claim: a profile created over HTTP is immediately bookable."""
        host = f.make_user(self.tenant)
        response = self._create(
            host, [{"weekday": d, "start_time": "09:00:00", "end_time": "17:00:00"} for d in range(7)]
        )

        self.assertEqual(response.status_code, 201, response.data)
        profile = AvailabilityProfile.objects.get(user=host)
        windows = AvailabilityWindow.objects.filter(profile=profile)
        self.assertGreater(windows.count(), 0, "on_commit -> task -> windows chain is broken")
        self.assertTrue(all(w.user_id == host.id for w in windows))
        self.assertTrue(all(w.tenant_id == self.tenant.id for w in windows))

    def test_create_accepts_an_overnight_rule(self):
        host = f.make_user(self.tenant)
        response = self._create(
            host, [{"weekday": 0, "start_time": "22:00:00", "end_time": "06:00:00"}]
        )

        self.assertEqual(response.status_code, 201, response.data)
        window = AvailabilityWindow.objects.filter(user=host).first()
        self.assertIsNotNone(window)
        self.assertEqual(window.end_utc - window.start_utc, timedelta(hours=8))

    def test_create_rejects_a_wrap_collision(self):
        host = f.make_user(self.tenant)
        response = self._create(
            host,
            [
                {"weekday": 0, "start_time": "22:00:00", "end_time": "06:00:00"},
                {"weekday": 1, "start_time": "02:00:00", "end_time": "09:00:00"},
            ],
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("rules", response.data["errors"])
        self.assertFalse(AvailabilityWindow.objects.filter(user=host).exists())

    def test_failed_create_materializes_nothing(self):
        """A rejected write must not leave windows behind. on_commit hooks fire
        after COMMIT, so a rolled-back request must never reach the task."""
        host = f.make_user(self.tenant)
        f.make_timezone()  # keep tz valid; the failure comes from the rules
        response = self._create(
            host,
            [
                {"weekday": 0, "start_time": "09:00:00", "end_time": "17:00:00"},
                {"weekday": 0, "start_time": "16:00:00", "end_time": "18:00:00"},
            ],
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(AvailabilityWindow.objects.filter(user=host).count(), 0)

    # --- update ---------------------------------------------------------------

    def _patch(self, profile, body):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.patch(
                f"{PROFILES_URL}{profile.id}/", body, format="json"
            )

    def test_editing_rules_replaces_the_windows(self):
        host = f.make_user(self.tenant)
        self._create(host, [{"weekday": d, "start_time": "09:00:00", "end_time": "17:00:00"} for d in range(7)])
        profile = AvailabilityProfile.objects.get(user=host)

        response = self._patch(
            profile,
            {"rules": [{"weekday": d, "start_time": "11:00:00", "end_time": "15:00:00"} for d in range(7)]},
        )

        self.assertEqual(response.status_code, 200, response.data)
        hours = set(
            AvailabilityWindow.objects.filter(profile=profile).values_list(
                "start_utc__hour", flat=True
            )
        )
        self.assertEqual(hours, {11}, "stale 09:00 windows survived the edit")

    def test_changing_only_the_timezone_re_anchors_windows(self):
        """No rule changed, so a naive implementation skips regeneration — and every
        window then sits at the wrong absolute instant."""
        host = f.make_user(self.tenant)
        self._create(host, [{"weekday": d, "start_time": "09:00:00", "end_time": "17:00:00"} for d in range(7)])
        profile = AvailabilityProfile.objects.get(user=host)
        before = set(
            AvailabilityWindow.objects.filter(profile=profile).values_list(
                "start_utc", flat=True
            )
        )

        kolkata = f.make_timezone("Asia/Kolkata", "UTC+05:30", "Kolkata")
        response = self._patch(profile, {"timezone": str(kolkata.id)})

        self.assertEqual(response.status_code, 200, response.data)
        after = set(
            AvailabilityWindow.objects.filter(profile=profile).values_list(
                "start_utc", flat=True
            )
        )
        self.assertTrue(after)
        self.assertNotEqual(before, after, "timezone change did not re-anchor the windows")
        # 09:00 IST is 03:30Z.
        sample = AvailabilityWindow.objects.filter(profile=profile).first()
        self.assertEqual((sample.start_utc.hour, sample.start_utc.minute), (3, 30))

    def test_deactivating_a_profile_purges_its_windows(self):
        """is_active previously did nothing to availability — the base manager
        filters is_deleted, not is_active."""
        host = f.make_user(self.tenant)
        self._create(host, [{"weekday": d, "start_time": "09:00:00", "end_time": "17:00:00"} for d in range(7)])
        profile = AvailabilityProfile.objects.get(user=host)
        self.assertTrue(AvailabilityWindow.objects.filter(profile=profile).exists())

        response = self._patch(profile, {"is_active": False})

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(AvailabilityWindow.objects.filter(profile=profile).count(), 0)

    def test_deleting_a_profile_purges_its_windows(self):
        host = f.make_user(self.tenant)
        self._create(host, [{"weekday": d, "start_time": "09:00:00", "end_time": "17:00:00"} for d in range(7)])
        profile = AvailabilityProfile.objects.get(user=host)

        response = self.client.delete(f"{PROFILES_URL}{profile.id}/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(AvailabilityWindow.objects.filter(profile=profile).count(), 0)


@REQUIRES_PG
class OvernightBookingOverHttpTests(APITestCase):
    """The whole feature, end to end over HTTP: author a night shift, list slots
    across midnight, book one."""

    def setUp(self):
        self.tenant = f.make_tenant()
        self.actor = f.make_user(self.tenant)
        self.tz = f.make_timezone()
        self.template = f.make_template(
            self.tenant, duration_minutes=30, slot_granularity_minutes=30, min_notice_minutes=0
        )
        self.host = f.make_user(self.tenant)
        UserEventSkill.objects.create(
            tenant=self.tenant, user=self.host, event_template=self.template
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.actor)

        # Anchor on a real future date so min-notice/horizon policy behaves as in
        # production; the shift is on whatever weekday that lands on.
        self.night = (timezone.now() + timedelta(days=3)).date()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                PROFILES_URL,
                {
                    "user": str(self.host.id),
                    "timezone": str(self.tz.id),
                    "rules": [
                        {
                            "weekday": self.night.weekday(),
                            "start_time": "22:00:00",
                            "end_time": "06:00:00",
                        }
                    ],
                },
                format="json",
            )
        self.assertEqual(response.status_code, 201, response.data)

    def test_slots_and_booking_across_midnight(self):
        start_utc, end_utc = f.utc_day(self.night)
        response = self.client.get(
            SLOTS_URL,
            {
                "event_template": str(self.template.id),
                "start_utc": start_utc.isoformat(),
                "end_utc": end_utc.isoformat(),
                "target_host": str(self.host.id),
            },
        )
        self.assertEqual(response.status_code, 200, response.data)
        starts = [s["start"] for s in response.data["data"]["slots"]]
        self.assertTrue(starts, "no slots offered for the night shift")

        # The last slot of the requested date starts at 23:30 and ends at 00:00 —
        # the case the old engine could not express at all.
        self.assertTrue(
            any(s.startswith(f"{self.night.isoformat()}T23:30") for s in starts),
            f"23:30 slot missing from {starts[-3:]}",
        )

        with self.captureOnCommitCallbacks(execute=True):
            booked = self.client.post(
                BOOKINGS_URL,
                {
                    "event_template": str(self.template.id),
                    "start_time": f"{self.night.isoformat()}T23:45:00Z",
                    "target_host": str(self.host.id),
                    "participants": [{"name": "Cust", "email": "cust@example.com"}],
                },
                format="json",
            )
        self.assertEqual(booked.status_code, 201, booked.data)
        self.assertEqual(booked.data["data"]["status"], "SCHEDULED")

    def test_midday_booking_is_still_refused(self):
        """Overnight support must not become 'anything goes'.

        409, not 400: no eligible host for a specific requested time is a slot
        conflict, which is what the caller should retry against fresh slots.
        """
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                BOOKINGS_URL,
                {
                    "event_template": str(self.template.id),
                    "start_time": f"{self.night.isoformat()}T12:00:00Z",
                    "target_host": str(self.host.id),
                    "participants": [{"name": "Cust", "email": "cust@example.com"}],
                },
                format="json",
            )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertIn("no longer available", response.data["message"])
