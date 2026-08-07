"""
Business hours API.

A singleton, so the shapes matter more than the verbs: `schedule` always comes
back as seven entries even though closed days store nothing, and a write replaces
the whole week rather than merging into it.

The validation worth having is the circular-week overlap check. Sorting each
weekday independently cannot see Monday's overnight block colliding with Tuesday
morning, and it certainly cannot see Sunday night colliding with Monday.
"""

from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.models.business_hours import BusinessHours, BusinessHoursInterval
from onboard.models.timezone import Timezone
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models import CustomUser as User

URL = "/api/v1/core/business-hours/"


class BusinessHoursAPITestCase(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-bh")
        self.other_tenant = Tenant.objects.create(name="Globex", subdomain="globex-bh")
        self.kolkata, _ = Timezone.objects.get_or_create(
            timezone="Asia/Kolkata",
            defaults={"offset": "UTC+05:30", "region": "Asia"},
        )

        self.user = User.objects.create_user(
            username="bh-alex", email="bh-alex@example.com", password="password"
        )
        self.other_user = User.objects.create_user(
            username="bh-bob", email="bh-bob@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)
        TenantUserMapping.objects.create(user=self.other_user, tenant=self.other_tenant)

        self.client_alex = APIClient()
        self.client_alex.force_authenticate(user=self.user)
        self.client_bob = APIClient()
        self.client_bob.force_authenticate(user=self.other_user)

    def day(self, weekday, *pairs):
        return {
            "weekday": weekday,
            "intervals": [
                {"opens_at": opens, "closes_at": closes} for opens, closes in pairs
            ],
        }

    def patch(self, client=None, **body):
        return (client or self.client_alex).patch(URL, body, format="json")


class ReadTests(BusinessHoursAPITestCase):
    def test_the_first_read_creates_the_row(self):
        """A fresh tenant gets a renderable grid, not a 404 the client has to
        special-case."""
        self.assertFalse(BusinessHours.objects.filter(tenant=self.tenant).exists())

        response = self.client_alex.get(URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(BusinessHours.objects.filter(tenant=self.tenant).exists())

    def test_reading_twice_does_not_create_a_second_row(self):
        self.client_alex.get(URL)
        self.client_alex.get(URL)
        self.assertEqual(BusinessHours.objects.filter(tenant=self.tenant).count(), 1)

    def test_schedule_is_always_seven_days_in_order(self):
        """Closed days store no rows, but the grid has seven lines regardless —
        leaving the client to synthesise them is how Sunday goes missing."""
        response = self.client_alex.get(URL)

        schedule = response.data["data"]["schedule"]
        self.assertEqual(len(schedule), 7)
        self.assertEqual([day["weekday"] for day in schedule], list(range(7)))
        self.assertEqual(schedule[0]["day"], "Monday")
        self.assertEqual(schedule[6]["day"], "Sunday")

    def test_a_day_with_no_intervals_reports_closed(self):
        response = self.client_alex.get(URL)
        for day in response.data["data"]["schedule"]:
            self.assertFalse(day["is_open"])
            self.assertEqual(day["intervals"], [])

    def test_authentication_is_required(self):
        self.assertEqual(
            APIClient().get(URL).status_code, status.HTTP_401_UNAUTHORIZED
        )


class WriteTests(BusinessHoursAPITestCase):
    def test_setting_a_weekly_pattern(self):
        response = self.patch(
            timezone=str(self.kolkata.id),
            schedule=[self.day(0, ("09:00", "17:00")), self.day(1, ("09:00", "17:00"))],
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        schedule = response.data["data"]["schedule"]
        self.assertTrue(schedule[0]["is_open"])
        self.assertEqual(schedule[0]["intervals"][0]["opens_at"], "09:00:00")
        self.assertFalse(schedule[2]["is_open"])

    def test_a_split_shift_round_trips(self):
        """The case the reference design could not express at all."""
        response = self.patch(
            timezone=str(self.kolkata.id),
            schedule=[self.day(0, ("09:00", "13:00"), ("14:00", "18:00"))],
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        monday = response.data["data"]["schedule"][0]
        self.assertEqual(len(monday["intervals"]), 2)
        self.assertEqual(monday["intervals"][1]["opens_at"], "14:00:00")

    def test_an_overnight_block_is_accepted(self):
        response = self.patch(
            timezone=str(self.kolkata.id),
            schedule=[self.day(5, ("22:00", "02:00"))],
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_writing_replaces_the_whole_week(self):
        """Not a merge: the client sends the grid it wants."""
        self.patch(
            timezone=str(self.kolkata.id),
            schedule=[self.day(0, ("09:00", "17:00")), self.day(1, ("09:00", "17:00"))],
        )
        response = self.patch(schedule=[self.day(0, ("10:00", "16:00"))])

        schedule = response.data["data"]["schedule"]
        self.assertEqual(schedule[0]["intervals"][0]["opens_at"], "10:00:00")
        self.assertFalse(schedule[1]["is_open"], "Tuesday should have been cleared")

    def test_an_empty_schedule_closes_every_day(self):
        """An empty list is a deliberate "closed all week" and must clear the
        week — unlike omitting the key, which leaves it alone."""
        self.patch(timezone=str(self.kolkata.id), schedule=[self.day(0, ("09:00", "17:00"))])

        response = self.patch(schedule=[])

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(BusinessHoursInterval.objects.count(), 0)

    def test_omitting_the_schedule_leaves_it_alone(self):
        self.patch(timezone=str(self.kolkata.id), schedule=[self.day(0, ("09:00", "17:00"))])

        response = self.patch(closed_message="Back Monday")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["data"]["schedule"][0]["is_open"])
        self.assertEqual(response.data["data"]["closed_message"], "Back Monday")

    def test_the_master_switch_is_writable(self):
        response = self.patch(timezone=str(self.kolkata.id), is_open=False)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(response.data["data"]["is_open"])

    def test_put_and_delete_are_not_allowed(self):
        """A singleton has nothing to create or destroy."""
        self.assertEqual(
            self.client_alex.put(URL, {}, format="json").status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )
        self.assertEqual(
            self.client_alex.delete(URL).status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )


class OverlapValidationTests(BusinessHoursAPITestCase):
    def _rejects(self, schedule):
        response = self.patch(timezone=str(self.kolkata.id), schedule=schedule)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        return response

    def test_two_blocks_on_one_day_may_not_overlap(self):
        self._rejects([self.day(0, ("09:00", "13:00"), ("12:00", "18:00"))])

    def test_an_overnight_block_may_not_collide_with_the_next_morning(self):
        """Per-weekday sorting cannot see this: Monday 22:00-02:00 runs into
        Tuesday's 01:00 opening."""
        self._rejects([self.day(0, ("22:00", "02:00")), self.day(1, ("01:00", "09:00"))])

    def test_sunday_night_may_not_collide_with_monday_morning(self):
        """The week is circular — this is the case a linear scan always misses."""
        self._rejects([self.day(6, ("22:00", "03:00")), self.day(0, ("02:00", "09:00"))])

    def test_a_weekday_may_not_appear_twice(self):
        response = self._rejects(
            [self.day(0, ("09:00", "12:00")), self.day(0, ("13:00", "17:00"))]
        )
        self.assertIn("more than once", str(response.data["errors"]))

    def test_more_than_a_week_of_coverage_is_rejected(self):
        self._rejects([self.day(weekday, ("00:00", "00:00")) for weekday in range(7)]
                      + [self.day(0, ("09:00", "10:00"))])

    def test_seven_full_days_are_allowed(self):
        """24/7 is exactly a week, not more than one."""
        response = self.patch(
            timezone=str(self.kolkata.id),
            schedule=[self.day(weekday, ("00:00", "00:00")) for weekday in range(7)],
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_adjacent_blocks_are_allowed(self):
        """Closing time is exclusive, so 13:00 then 13:00 does not overlap."""
        response = self.patch(
            timezone=str(self.kolkata.id),
            schedule=[self.day(0, ("09:00", "13:00"), ("13:00", "18:00"))],
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_a_weekday_outside_zero_to_six_is_rejected(self):
        self._rejects([self.day(7, ("09:00", "17:00"))])


class IsolationTests(BusinessHoursAPITestCase):
    def test_each_tenant_gets_its_own_schedule(self):
        self.patch(timezone=str(self.kolkata.id), schedule=[self.day(0, ("09:00", "17:00"))])

        theirs = self.client_bob.get(URL)

        self.assertEqual(theirs.status_code, status.HTTP_200_OK)
        self.assertFalse(theirs.data["data"]["schedule"][0]["is_open"])
        self.assertNotEqual(
            theirs.data["data"]["id"], self.client_alex.get(URL).data["data"]["id"]
        )

    def test_one_tenants_write_does_not_touch_anothers(self):
        self.patch(timezone=str(self.kolkata.id), schedule=[self.day(0, ("09:00", "17:00"))])
        self.patch(
            client=self.client_bob,
            timezone=str(self.kolkata.id),
            schedule=[self.day(3, ("10:00", "14:00"))],
        )

        mine = self.client_alex.get(URL).data["data"]["schedule"]
        self.assertTrue(mine[0]["is_open"])
        self.assertFalse(mine[3]["is_open"])

    def test_the_tenant_is_not_taken_from_the_body(self):
        response = self.patch(
            timezone=str(self.kolkata.id), tenant=str(self.other_tenant.id)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            BusinessHours.objects.get(pk=response.data["data"]["id"]).tenant_id,
            self.tenant.id,
        )
