"""`GET /core/bookings/daily-count/` — how many bookings fall on each date.

Three things carry the endpoint and each has its own failure mode:

  * **The date a booking belongs to is a function of timezone.** A 21:00 IST
    appointment is 15:30 UTC the same day; a 02:00 one is the *previous* UTC day.
    Bucketing in UTC silently moves a slice of every tenant's bookings.
  * **Grouping is fragile.** `Booking.Meta.ordering` is `-start_time`, which joins
    the GROUP BY unless it is explicitly cleared — turning "3 bookings on Monday"
    into three rows of 1.
  * **The range is capped at 61 days** because the response is zero-filled, so its
    size is the range length rather than the number of bookings.
"""

from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from unittest import skipUnless
from zoneinfo import ZoneInfo

from django.contrib.auth.hashers import make_password
from django.db import connection
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.models.booking import Booking
from core.models.booking_participant import BookingParticipant
from core.tests.scheduling import factories as f
from user.models.CustomUser import CustomUser as User
from user.models.agent_token import AgentToken

URL = "/api/v1/core/bookings/daily-count/"
UTC = dt_timezone.utc
KOLKATA = ZoneInfo("Asia/Kolkata")


@skipUnless(connection.vendor == "postgresql", "Scheduling tests require PostgreSQL.")
class BookingDailyCountTests(APITestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.actor = f.make_user(self.tenant)
        self.template = f.make_template(self.tenant)
        self.host = f.make_host(self.tenant, self.template)
        self.other_host = f.make_host(self.tenant, self.template)

        self.client.force_authenticate(user=self.actor)

    # --- helpers --------------------------------------------------------------

    def _book(self, start, host=None, status_field=Booking.Status.SCHEDULED, tenant=None):
        """A booking placed directly, bypassing the service layer.

        These tests are about counting rows by date, not about booking policy —
        going through the service would drag in notice windows and host
        availability for no gain, and would make some of the dates below
        unbookable outright.
        """
        booking = Booking.objects.create(
            tenant=tenant or self.tenant,
            event_template=self.template,
            title="Meeting",
            primary_host=host or self.host,
            start_time=start,
            end_time=start + timedelta(minutes=30),
            effective_start_time=start,
            effective_end_time=start + timedelta(minutes=30),
            status=status_field,
            hold_expires_at=(
                start if status_field == Booking.Status.HELD else None
            ),
        )
        BookingParticipant.objects.create(
            tenant=booking.tenant,
            booking=booking,
            name="Customer",
            email="customer@example.com",
        )
        return booking

    def _get(self, **params):
        return self.client.get(URL, params)

    def _days(self, response):
        return {row["date"]: row["count"] for row in response.data["data"]["days"]}

    # --- counting -------------------------------------------------------------

    def test_it_counts_bookings_per_date(self):
        self._book(datetime(2027, 4, 5, 9, tzinfo=UTC))
        self._book(datetime(2027, 4, 5, 11, tzinfo=UTC))
        self._book(datetime(2027, 4, 7, 9, tzinfo=UTC))

        response = self._get(
            start_date="2027-04-05", end_date="2027-04-07", timezone="UTC"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self._days(response), {"2027-04-05": 2, "2027-04-06": 0, "2027-04-07": 1}
        )
        self.assertEqual(response.data["data"]["total"], 3)

    def test_a_day_with_several_bookings_is_one_row(self):
        """Guards the GROUP BY.

        `Booking.Meta.ordering` is `-start_time`. If it is not cleared before
        `.values().annotate()` it becomes a grouping key, and this returns three
        rows of `count: 1` for the same date instead of one row of 3 — which still
        serializes cleanly and still sums to 3.
        """
        for hour in (9, 11, 15):
            self._book(datetime(2027, 4, 5, hour, tzinfo=UTC))

        response = self._get(
            start_date="2027-04-05", end_date="2027-04-05", timezone="UTC"
        )

        self.assertEqual(len(response.data["data"]["days"]), 1)
        self.assertEqual(response.data["data"]["days"][0]["count"], 3)

    def test_empty_dates_are_returned_as_zero(self):
        response = self._get(
            start_date="2027-04-01", end_date="2027-04-10", timezone="UTC"
        )

        days = response.data["data"]["days"]
        self.assertEqual(len(days), 10)
        self.assertEqual([row["count"] for row in days], [0] * 10)
        self.assertEqual(response.data["data"]["total"], 0)
        # In order, so a client can render them without sorting.
        self.assertEqual(days[0]["date"], "2027-04-01")
        self.assertEqual(days[-1]["date"], "2027-04-10")

    def test_bookings_outside_the_range_are_excluded(self):
        self._book(datetime(2027, 4, 4, 23, 59, tzinfo=UTC))
        self._book(datetime(2027, 4, 5, 9, tzinfo=UTC))
        self._book(datetime(2027, 4, 8, 0, 1, tzinfo=UTC))

        response = self._get(
            start_date="2027-04-05", end_date="2027-04-07", timezone="UTC"
        )

        self.assertEqual(response.data["data"]["total"], 1)

    def test_both_bounds_are_inclusive(self):
        self._book(datetime(2027, 4, 5, 9, tzinfo=UTC))
        self._book(datetime(2027, 4, 7, 9, tzinfo=UTC))

        response = self._get(
            start_date="2027-04-05", end_date="2027-04-07", timezone="UTC"
        )

        self.assertEqual(self._days(response)["2027-04-05"], 1)
        self.assertEqual(self._days(response)["2027-04-07"], 1)

    # --- timezone -------------------------------------------------------------

    def test_the_date_a_booking_falls_on_depends_on_the_zone(self):
        """The whole reason `timezone` is not presentational.

        2027-04-05 20:00 UTC is 2027-04-06 01:30 in Kolkata. One instant, two
        different answers to "which day is this booked on", and the tenant's is
        the one that matters.
        """
        self._book(datetime(2027, 4, 5, 20, tzinfo=UTC))

        in_utc = self._get(start_date="2027-04-05", end_date="2027-04-06", timezone="UTC")
        in_kolkata = self._get(
            start_date="2027-04-05", end_date="2027-04-06", timezone="Asia/Kolkata"
        )

        self.assertEqual(self._days(in_utc), {"2027-04-05": 1, "2027-04-06": 0})
        self.assertEqual(self._days(in_kolkata), {"2027-04-05": 0, "2027-04-06": 1})

    def test_a_local_midnight_boundary_is_counted_in_the_local_zone(self):
        """00:30 IST on the 6th — comfortably the previous day in UTC (19:00 on the
        5th), and the range does not include the 5th at all. Counting in UTC would
        drop it entirely rather than merely misplace it."""
        self._book(datetime(2027, 4, 6, 0, 30, tzinfo=KOLKATA))

        response = self._get(
            start_date="2027-04-06", end_date="2027-04-06", timezone="Asia/Kolkata"
        )

        self.assertEqual(response.data["data"]["total"], 1)

    def test_it_defaults_to_the_callers_own_timezone(self):
        self.actor.timezone = f.make_timezone("Asia/Kolkata", "UTC+05:30", "Kolkata")
        self.actor.save(update_fields=["timezone"])
        self._book(datetime(2027, 4, 5, 20, tzinfo=UTC))  # 06 Apr 01:30 IST

        response = self._get(start_date="2027-04-05", end_date="2027-04-06")

        self.assertEqual(response.data["data"]["timezone"], "Asia/Kolkata")
        self.assertEqual(self._days(response), {"2027-04-05": 0, "2027-04-06": 1})

    def test_an_unknown_zone_is_rejected_rather_than_silently_ignored(self):
        response = self._get(
            start_date="2027-04-05", end_date="2027-04-06", timezone="Mars/Olympus"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("timezone", response.data["errors"])

    # --- range validation -----------------------------------------------------

    def test_the_maximum_range_of_61_days_is_accepted(self):
        start = date(2027, 4, 1)
        response = self._get(
            start_date=start.isoformat(),
            end_date=(start + timedelta(days=60)).isoformat(),
            timezone="UTC",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data["data"]["days"]), 61)

    def test_62_days_is_rejected(self):
        """Off-by-one guard: both bounds are inclusive, so 61 days apart is 62 days
        of data. A caller asking for exactly 61 must not be refused."""
        start = date(2027, 4, 1)
        response = self._get(
            start_date=start.isoformat(),
            end_date=(start + timedelta(days=61)).isoformat(),
            timezone="UTC",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("62", str(response.data["errors"]))

    def test_end_before_start_is_rejected(self):
        response = self._get(start_date="2027-04-10", end_date="2027-04-05")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("end_date", response.data["errors"])

    def test_both_dates_are_required(self):
        response = self._get(start_date="2027-04-05")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("end_date", response.data["errors"])

    # --- scoping --------------------------------------------------------------

    def test_another_tenants_bookings_are_not_counted(self):
        other = f.make_tenant()
        other_template = f.make_template(other)
        other_host = f.make_host(other, other_template)
        Booking.objects.create(
            tenant=other,
            event_template=other_template,
            title="Theirs",
            primary_host=other_host,
            start_time=datetime(2027, 4, 5, 9, tzinfo=UTC),
            end_time=datetime(2027, 4, 5, 9, 30, tzinfo=UTC),
            effective_start_time=datetime(2027, 4, 5, 9, tzinfo=UTC),
            effective_end_time=datetime(2027, 4, 5, 9, 30, tzinfo=UTC),
        )
        self._book(datetime(2027, 4, 5, 9, tzinfo=UTC))

        response = self._get(
            start_date="2027-04-05", end_date="2027-04-05", timezone="UTC"
        )

        self.assertEqual(response.data["data"]["total"], 1)

    def test_soft_deleted_bookings_are_not_counted(self):
        booking = self._book(datetime(2027, 4, 5, 9, tzinfo=UTC))
        booking.soft_delete(user=self.actor)

        response = self._get(
            start_date="2027-04-05", end_date="2027-04-05", timezone="UTC"
        )

        self.assertEqual(response.data["data"]["total"], 0)

    def test_the_list_filters_apply(self):
        """The counts have to be the counts of what `GET /bookings/` would return,
        or a chart and the list beneath it disagree."""
        self._book(datetime(2027, 4, 5, 9, tzinfo=UTC), status_field=Booking.Status.CONFIRMED)
        self._book(datetime(2027, 4, 5, 11, tzinfo=UTC), status_field=Booking.Status.CANCELED)

        unfiltered = self._get(
            start_date="2027-04-05", end_date="2027-04-05", timezone="UTC"
        )
        confirmed = self._get(
            start_date="2027-04-05",
            end_date="2027-04-05",
            timezone="UTC",
            status="CONFIRMED",
        )

        self.assertEqual(unfiltered.data["data"]["total"], 2)
        self.assertEqual(confirmed.data["data"]["total"], 1)

    def test_it_can_be_narrowed_to_a_single_host(self):
        self._book(datetime(2027, 4, 5, 9, tzinfo=UTC), host=self.host)
        self._book(datetime(2027, 4, 5, 9, tzinfo=UTC), host=self.other_host)

        response = self._get(
            start_date="2027-04-05",
            end_date="2027-04-05",
            timezone="UTC",
            primary_host=str(self.host.id),
        )

        self.assertEqual(response.data["data"]["total"], 1)

    def test_an_agent_token_is_refused(self):
        """Reporting is not the voicebot's job, and this action is not on its
        allowlist — so it must 403 rather than fall through to an unscoped read."""
        runner = User.objects.create_user(
            username="count_vb",
            email="count-vb@example.com",
            password="password",
            user_type=User.UserType.VB_AGENT,
        )
        AgentToken.objects.create(
            user=runner,
            name="Count",
            prefix="vb_count",
            key=make_password("count_secret"),
            never_expires=True,
        )
        agent = APIClient()
        agent.credentials(HTTP_AUTHORIZATION="Bearer vb_count_count_secret")

        response = agent.get(
            URL,
            {"start_date": "2027-04-05", "end_date": "2027-04-05"},
            HTTP_X_TENANT_ID=str(self.tenant.id),
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_it_requires_authentication(self):
        anonymous = APIClient()

        response = anonymous.get(
            URL, {"start_date": "2027-04-05", "end_date": "2027-04-05"}
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
