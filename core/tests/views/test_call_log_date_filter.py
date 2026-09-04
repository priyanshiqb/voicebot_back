"""
The `start_date` / `end_date` range on the call log list.

Both bounds are inclusive UTC calendar days, and both match on `created_at` rather
than `started_at`. That choice is what most of these tests are really about:
`started_at` is nullable, so ranging on it would quietly drop logs the runner posted
without one, and the list would disagree with the dashboard — which ranges on
`created_at` for exactly that reason.
"""

from datetime import datetime, timedelta, timezone as dt_timezone

from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from core.models.agent import Agent
from core.models.agent_call_log import AgentCallLog
from django.utils import timezone
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models.CustomUser import CustomUser as User
from user.models.user_session import UserSession

LIST_URL = "/api/v1/core/agent-call-logs/"


def utc(year, month, day, hour=0, minute=0, second=0, microsecond=0):
    return datetime(
        year, month, day, hour, minute, second, microsecond, tzinfo=dt_timezone.utc
    )


class CallLogDateRangeFilterTests(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-dates")
        self.user = User.objects.create_user(
            username="dana",
            email="dana@example.com",
            password="password",
            user_type=User.UserType.USER,
        )
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)
        self.agent = Agent.objects.create(
            tenant=self.tenant, bot_name="Bot", persona_name="P"
        )

        refresh = RefreshToken.for_user(self.user)
        access = refresh.access_token
        UserSession.objects.create(
            user=self.user,
            access_jti=access.get("jti"),
            refresh_jti="dummy_refresh_jti",
            device_name="Test Client",
            ip_address="127.0.0.1",
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def _log(self, call_id, created_at, started_at=None):
        """A call log filed at `created_at`.

        `created_at` is `auto_now_add`, so it cannot be passed to `create()` — the
        row is written first and the timestamp forced afterwards with an UPDATE that
        bypasses `save()`.
        """
        log = AgentCallLog.objects.create(
            tenant=self.tenant,
            agent=self.agent,
            provider_call_id=call_id,
            caller_number="+14155557001",
            direction=AgentCallLog.CallDirection.INBOUND,
            duration_seconds=30,
            started_at=started_at,
        )
        AgentCallLog.objects.filter(pk=log.pk).update(created_at=created_at)
        return log

    def _ids(self, response):
        return {row["provider_call_id"] for row in response.data["data"]}

    def test_start_date_is_inclusive_from_midnight(self):
        self._log("before", utc(2026, 6, 22, 23, 59, 59))
        self._log("on-boundary", utc(2026, 6, 23, 0, 0, 0))

        response = self.client.get(LIST_URL, {"start_date": "2026-06-23"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self._ids(response), {"on-boundary"})

    def test_end_date_includes_the_whole_day(self):
        """The end bound is a day, not the midnight that opens it."""
        self._log("late-in-day", utc(2026, 6, 23, 23, 59, 59, 999999))
        self._log("next-day", utc(2026, 6, 24, 0, 0, 0))

        response = self.client.get(LIST_URL, {"end_date": "2026-06-23"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self._ids(response), {"late-in-day"})

    def test_both_bounds_narrow_to_the_range(self):
        self._log("too-early", utc(2026, 6, 20, 12))
        self._log("inside-start", utc(2026, 6, 21, 0, 0, 0))
        self._log("inside-middle", utc(2026, 6, 22, 12))
        self._log("inside-end", utc(2026, 6, 23, 18))
        self._log("too-late", utc(2026, 6, 24, 1))

        response = self.client.get(
            LIST_URL, {"start_date": "2026-06-21", "end_date": "2026-06-23"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            self._ids(response), {"inside-start", "inside-middle", "inside-end"}
        )

    def test_log_without_started_at_is_still_in_range(self):
        """The whole reason the filter reads `created_at`.

        Ranging on `started_at` would drop this row from every date-filtered view.
        """
        self._log("no-start-time", utc(2026, 6, 23, 10), started_at=None)

        response = self.client.get(
            LIST_URL, {"start_date": "2026-06-23", "end_date": "2026-06-23"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self._ids(response), {"no-start-time"})

    def test_range_is_read_on_created_at_not_started_at(self):
        """A call that happened before the window but was logged inside it is in.

        Asserted explicitly so that "filter on `created_at`" is a decision a future
        change has to break a test to reverse, rather than an accident.
        """
        self._log(
            "logged-late", created_at=utc(2026, 6, 23, 9), started_at=utc(2026, 6, 1, 9)
        )

        response = self.client.get(
            LIST_URL, {"start_date": "2026-06-23", "end_date": "2026-06-23"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self._ids(response), {"logged-late"})

    def test_no_dates_returns_everything(self):
        self._log("old", utc(2020, 1, 1))
        self._log("new", utc(2026, 6, 23))

        response = self.client.get(LIST_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self._ids(response), {"old", "new"})

    def test_inverted_range_is_rejected(self):
        response = self.client.get(
            LIST_URL, {"start_date": "2026-06-23", "end_date": "2026-06-21"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_malformed_date_is_rejected(self):
        response = self.client.get(LIST_URL, {"start_date": "23-06-2026"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_dates_combine_with_other_filters(self):
        self._log("inbound-in-range", utc(2026, 6, 23, 10))
        outbound = self._log("outbound-in-range", utc(2026, 6, 23, 11))
        AgentCallLog.objects.filter(pk=outbound.pk).update(
            direction=AgentCallLog.CallDirection.OUTBOUND
        )

        response = self.client.get(
            LIST_URL,
            {
                "start_date": "2026-06-23",
                "end_date": "2026-06-23",
                "direction": "outbound",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self._ids(response), {"outbound-in-range"})
