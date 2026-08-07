"""Date filtering on `GET /core/bookings/`.

The daily-count endpoint says "4 bookings on the 5th"; clicking that bar has to
return those same 4. That agreement is the point of this module — the two share
`filter_local_date_range` precisely so they cannot drift, and
`test_the_list_agrees_with_the_count` is what holds them together.

Local **dates** and UTC **instants** are both offered because "show me Tuesday"
and "show me 14:00-17:00" are different questions, and answering the first with
instants makes every client reimplement timezone maths.
"""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest import skipUnless
from zoneinfo import ZoneInfo

from django.db import connection
from rest_framework import status
from rest_framework.test import APITestCase

from core.models.booking import Booking
from core.models.booking_participant import BookingParticipant
from core.tests.scheduling import factories as f

LIST_URL = "/api/v1/core/bookings/"
COUNT_URL = "/api/v1/core/bookings/daily-count/"
UTC = dt_timezone.utc
KOLKATA = ZoneInfo("Asia/Kolkata")


@skipUnless(connection.vendor == "postgresql", "Scheduling tests require PostgreSQL.")
class BookingDateFilterTests(APITestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.actor = f.make_user(self.tenant)
        self.template = f.make_template(self.tenant)
        self.host = f.make_host(self.tenant, self.template)
        # A second host, so tests that book back-to-back across a midnight
        # boundary do not trip core_booking_no_overlap_per_host.
        self.other_host = f.make_host(self.tenant, self.template)
        self.client.force_authenticate(user=self.actor)

    def _book(self, start, status_field=Booking.Status.SCHEDULED, host=None):
        booking = Booking.objects.create(
            tenant=self.tenant,
            event_template=self.template,
            title="Meeting",
            primary_host=host or self.host,
            start_time=start,
            end_time=start + timedelta(minutes=30),
            effective_start_time=start,
            effective_end_time=start + timedelta(minutes=30),
            status=status_field,
            hold_expires_at=start if status_field == Booking.Status.HELD else None,
        )
        BookingParticipant.objects.create(
            tenant=self.tenant,
            booking=booking,
            name="Customer",
            email="customer@example.com",
        )
        return booking

    def _ids(self, response):
        return {row["id"] for row in response.data["data"]}

    # --- a single day ---------------------------------------------------------

    def test_date_returns_only_that_days_bookings(self):
        wanted = self._book(datetime(2027, 4, 5, 9, tzinfo=UTC))
        self._book(datetime(2027, 4, 4, 9, tzinfo=UTC))
        self._book(datetime(2027, 4, 6, 9, tzinfo=UTC))

        response = self.client.get(LIST_URL, {"date": "2027-04-05", "timezone": "UTC"})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self._ids(response), {str(wanted.id)})

    def test_a_day_includes_its_first_and_last_instant(self):
        first = self._book(datetime(2027, 4, 5, 0, 0, tzinfo=UTC))
        last = self._book(datetime(2027, 4, 5, 23, 59, tzinfo=UTC))
        self._book(datetime(2027, 4, 6, 0, 0, tzinfo=UTC), host=self.other_host)

        response = self.client.get(LIST_URL, {"date": "2027-04-05", "timezone": "UTC"})

        self.assertEqual(self._ids(response), {str(first.id), str(last.id)})

    # --- a period -------------------------------------------------------------

    def test_a_date_range_is_inclusive_at_both_ends(self):
        on_start = self._book(datetime(2027, 4, 5, 9, tzinfo=UTC))
        inside = self._book(datetime(2027, 4, 6, 9, tzinfo=UTC))
        on_end = self._book(datetime(2027, 4, 7, 9, tzinfo=UTC))
        self._book(datetime(2027, 4, 4, 23, 59, tzinfo=UTC))
        self._book(datetime(2027, 4, 8, 0, 1, tzinfo=UTC))

        response = self.client.get(
            LIST_URL,
            {"start_date": "2027-04-05", "end_date": "2027-04-07", "timezone": "UTC"},
        )

        self.assertEqual(
            self._ids(response), {str(on_start.id), str(inside.id), str(on_end.id)}
        )

    def test_each_bound_works_on_its_own(self):
        self._book(datetime(2027, 4, 4, 9, tzinfo=UTC))
        later = self._book(datetime(2027, 4, 9, 9, tzinfo=UTC))

        open_ended = self.client.get(
            LIST_URL, {"start_date": "2027-04-05", "timezone": "UTC"}
        )
        open_started = self.client.get(
            LIST_URL, {"end_date": "2027-04-05", "timezone": "UTC"}
        )

        self.assertEqual(self._ids(open_ended), {str(later.id)})
        self.assertEqual(len(open_started.data["data"]), 1)

    def test_the_list_range_is_not_capped_at_61_days(self):
        """The 61-day cap on daily-count exists because that response is
        zero-filled, so its size is the range length. This one is paginated, so
        the same limit would be an arbitrary obstruction."""
        self._book(datetime(2027, 4, 5, 9, tzinfo=UTC))

        response = self.client.get(
            LIST_URL,
            {"start_date": "2027-01-01", "end_date": "2027-12-31", "timezone": "UTC"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data["data"]), 1)

    # --- instants -------------------------------------------------------------

    def test_instant_bounds_select_a_window_inside_a_day(self):
        morning = self._book(datetime(2027, 4, 5, 9, tzinfo=UTC))
        afternoon = self._book(datetime(2027, 4, 5, 15, tzinfo=UTC))

        response = self.client.get(
            LIST_URL,
            {
                "start_time_after": "2027-04-05T12:00:00Z",
                "start_time_before": "2027-04-05T18:00:00Z",
            },
        )

        self.assertEqual(self._ids(response), {str(afternoon.id)})
        self.assertNotIn(str(morning.id), self._ids(response))

    def test_start_time_before_is_exclusive(self):
        """So consecutive windows tile instead of double-counting the boundary."""
        self._book(datetime(2027, 4, 5, 12, tzinfo=UTC))

        response = self.client.get(
            LIST_URL, {"start_time_before": "2027-04-05T12:00:00Z"}
        )

        self.assertEqual(len(response.data["data"]), 0)

    # --- timezone -------------------------------------------------------------

    def test_the_zone_decides_which_day_a_booking_is_on(self):
        """2027-04-05 20:00 UTC is 2027-04-06 01:30 in Kolkata."""
        booking = self._book(datetime(2027, 4, 5, 20, tzinfo=UTC))

        in_utc = self.client.get(LIST_URL, {"date": "2027-04-05", "timezone": "UTC"})
        in_kolkata = self.client.get(
            LIST_URL, {"date": "2027-04-06", "timezone": "Asia/Kolkata"}
        )

        self.assertEqual(self._ids(in_utc), {str(booking.id)})
        self.assertEqual(self._ids(in_kolkata), {str(booking.id)})

    def test_it_defaults_to_the_callers_timezone(self):
        self.actor.timezone = f.make_timezone("Asia/Kolkata", "UTC+05:30", "Kolkata")
        self.actor.save(update_fields=["timezone"])
        booking = self._book(datetime(2027, 4, 5, 20, tzinfo=UTC))  # 06 Apr IST

        on_the_6th = self.client.get(LIST_URL, {"date": "2027-04-06"})
        on_the_5th = self.client.get(LIST_URL, {"date": "2027-04-05"})

        self.assertEqual(self._ids(on_the_6th), {str(booking.id)})
        self.assertEqual(len(on_the_5th.data["data"]), 0)

    def test_an_unknown_zone_is_a_400_not_a_500(self):
        """`ZoneInfo` raises on an unknown key, and this value reaches it from a
        query parameter — so it has to be caught at the boundary."""
        response = self.client.get(
            LIST_URL, {"date": "2027-04-05", "timezone": "Mars/Olympus"}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # --- the property that matters --------------------------------------------

    def test_the_list_agrees_with_the_count(self):
        """Every day's count must equal the number of rows the list returns for
        that day, in the same zone. These are separate code paths over the same
        data, and a client renders one and then drills into the other."""
        for hour in (0, 9, 18, 23):
            self._book(datetime(2027, 4, 5, hour, 30, tzinfo=KOLKATA))
        self._book(datetime(2027, 4, 6, 2, tzinfo=KOLKATA))
        self._book(datetime(2027, 4, 4, 22, tzinfo=KOLKATA))

        counts = self.client.get(
            COUNT_URL,
            {
                "start_date": "2027-04-04",
                "end_date": "2027-04-06",
                "timezone": "Asia/Kolkata",
            },
        )

        self.assertEqual(counts.status_code, status.HTTP_200_OK, counts.data)
        for row in counts.data["data"]["days"]:
            listed = self.client.get(
                LIST_URL, {"date": row["date"], "timezone": "Asia/Kolkata"}
            )
            self.assertEqual(
                len(listed.data["data"]),
                row["count"],
                f"count and list disagree for {row['date']}",
            )
        self.assertEqual(counts.data["data"]["total"], 6)

    # --- regressions on the filterset swap ------------------------------------

    def test_the_existing_filters_still_work(self):
        """`filterset_fields` was replaced by `filterset_class`; the four filters
        it used to declare have to survive that."""
        confirmed = self._book(
            datetime(2027, 4, 5, 9, tzinfo=UTC), status_field=Booking.Status.CONFIRMED
        )
        self._book(
            datetime(2027, 4, 5, 11, tzinfo=UTC), status_field=Booking.Status.CANCELED
        )

        by_status = self.client.get(LIST_URL, {"status": "CONFIRMED"})
        by_host = self.client.get(LIST_URL, {"primary_host": str(self.host.id)})
        by_template = self.client.get(
            LIST_URL, {"event_template": str(self.template.id)}
        )
        by_source = self.client.get(LIST_URL, {"source": "INTERNAL"})

        self.assertEqual(self._ids(by_status), {str(confirmed.id)})
        self.assertEqual(len(by_host.data["data"]), 2)
        self.assertEqual(len(by_template.data["data"]), 2)
        self.assertEqual(len(by_source.data["data"]), 2)

    def test_date_filters_combine_with_the_others(self):
        wanted = self._book(
            datetime(2027, 4, 5, 9, tzinfo=UTC), status_field=Booking.Status.CONFIRMED
        )
        self._book(
            datetime(2027, 4, 5, 11, tzinfo=UTC), status_field=Booking.Status.CANCELED
        )
        self._book(
            datetime(2027, 4, 6, 9, tzinfo=UTC), status_field=Booking.Status.CONFIRMED
        )

        response = self.client.get(
            LIST_URL,
            {"date": "2027-04-05", "timezone": "UTC", "status": "CONFIRMED"},
        )

        self.assertEqual(self._ids(response), {str(wanted.id)})

    def test_ordering_still_applies_alongside_a_date_filter(self):
        """The date filter annotates and filters; `-start_time` still has to
        drive the order, not the annotation."""
        early = self._book(datetime(2027, 4, 5, 9, tzinfo=UTC))
        late = self._book(datetime(2027, 4, 5, 17, tzinfo=UTC))

        response = self.client.get(LIST_URL, {"date": "2027-04-05", "timezone": "UTC"})

        returned = [row["id"] for row in response.data["data"]]
        self.assertEqual(returned, [str(late.id), str(early.id)])

    def test_another_tenants_bookings_stay_invisible(self):
        other = f.make_tenant()
        other_template = f.make_template(other)
        other_host = f.make_host(other, other_template)
        start = datetime(2027, 4, 5, 9, tzinfo=UTC)
        Booking.objects.create(
            tenant=other,
            event_template=other_template,
            title="Theirs",
            primary_host=other_host,
            start_time=start,
            end_time=start + timedelta(minutes=30),
            effective_start_time=start,
            effective_end_time=start + timedelta(minutes=30),
        )
        mine = self._book(start)

        response = self.client.get(LIST_URL, {"date": "2027-04-05", "timezone": "UTC"})

        self.assertEqual(self._ids(response), {str(mine.id)})
