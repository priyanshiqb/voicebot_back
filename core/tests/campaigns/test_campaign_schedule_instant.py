"""
Scheduling a campaign from a UTC instant sent by the client.

The client converts the user's chosen local time to an instant and sends it with its
offset; the server stores it and applies no timezone. That replaced reading date/time
pickers in the tenant's timezone, which scheduled campaigns hours late for any tenant
with no timezone set. The deprecated pickers still work, and now honour the zone the
client sends with them.
"""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from rest_framework import status

from core.models.campaign import Campaign
from core.tests.campaigns.test_campaign_api import BASE, CampaignAPITestCase
from onboard.models.timezone import Timezone

UTC = dt_timezone.utc
# Far enough ahead to stay in the future; India has no daylight saving to cloud it.
START_UTC = datetime(2030, 1, 15, 7, 30, tzinfo=UTC)  # 13:00 in Asia/Kolkata


class CampaignScheduleInstantTests(CampaignAPITestCase):
    def _create(self, **fields):
        return self.client_alex.post(BASE, self._payload(**fields), format="json")

    def _give_tenant_timezone(self, name="Asia/Kolkata"):
        zone, _ = Timezone.objects.get_or_create(
            timezone=name, defaults={"offset": "UTC+05:30", "region": "India"}
        )
        self.tenant.timezone = zone
        self.tenant.save(update_fields=["timezone"])

    # ------------------------------------------------------------- the instant

    def test_an_instant_in_utc_is_stored_as_sent(self):
        response = self._create(
            scheduled_start_at="2030-01-15T07:30:00Z", schedule_timezone="Asia/Kolkata"
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        campaign = Campaign.objects.get(pk=response.data["data"]["id"])
        self.assertEqual(campaign.scheduled_start_at, START_UTC)
        self.assertEqual(
            response.data["data"]["start_local"],
            {"date": "2030-01-15", "time": "13:00", "timezone": "Asia/Kolkata"},
        )

    def test_an_instant_with_an_offset_is_the_same_moment(self):
        response = self._create(scheduled_start_at="2030-01-15T13:00:00+05:30")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        campaign = Campaign.objects.get(pk=response.data["data"]["id"])
        self.assertEqual(campaign.scheduled_start_at, START_UTC)

    def test_an_instant_without_an_offset_is_refused(self):
        """Read as UTC, "13:00" meant for India would start five and a half hours late."""
        response = self._create(scheduled_start_at="2030-01-15T13:00:00")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("scheduled_start_at", response.data["errors"])
        self.assertFalse(Campaign.objects.filter(name="Q1 Leads").exists())

    def test_an_instant_in_the_past_is_refused(self):
        response = self._create(scheduled_start_at="2020-01-01T00:00:00Z")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("scheduled_start_at", response.data["errors"])

    def test_the_tenant_timezone_is_not_applied_to_an_instant(self):
        self._give_tenant_timezone("Asia/Kolkata")

        response = self._create(scheduled_start_at="2030-01-15T13:00:00Z")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        campaign = Campaign.objects.get(pk=response.data["data"]["id"])
        self.assertEqual(
            campaign.scheduled_start_at, datetime(2030, 1, 15, 13, 0, tzinfo=UTC)
        )
        # No label was sent, so the start is shown in UTC and says so.
        self.assertEqual(response.data["data"]["start_local"]["timezone"], "UTC")

    def test_a_null_instant_leaves_the_campaign_unscheduled(self):
        response = self._create(scheduled_start_at=None)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIsNone(response.data["data"]["start_local"])
        self.assertIsNone(response.data["data"]["scheduled_start_at"])

    def test_an_unknown_timezone_is_refused(self):
        response = self._create(
            scheduled_start_at="2030-01-15T07:30:00Z", schedule_timezone="Mars/Olympus"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("schedule_timezone", response.data["errors"])

    def test_an_instant_and_the_pickers_together_are_refused(self):
        response = self._create(
            scheduled_start_at="2030-01-15T07:30:00Z",
            start_date="2030-01-15",
            start_time="13:00",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("scheduled_start_at", response.data["errors"])

    # ------------------------------------------------------------- deprecated pickers

    def test_pickers_are_read_in_the_timezone_the_client_sends(self):
        """The bug report: a tenant with no timezone, and the client's zone ignored."""
        self.assertIsNone(self.tenant.timezone)

        response = self._create(
            start_date="2030-01-15", start_time="13:00", schedule_timezone="Asia/Kolkata"
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        campaign = Campaign.objects.get(pk=response.data["data"]["id"])
        self.assertEqual(campaign.scheduled_start_at, START_UTC)
        self.assertEqual(campaign.schedule_timezone, "Asia/Kolkata")

    def test_pickers_without_a_timezone_still_use_the_tenants(self):
        self._give_tenant_timezone("Asia/Kolkata")

        response = self._create(start_date="2030-01-15", start_time="13:00")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        campaign = Campaign.objects.get(pk=response.data["data"]["id"])
        self.assertEqual(campaign.scheduled_start_at, START_UTC)

    # ------------------------------------------------------------- editing

    def test_a_scheduled_campaign_may_move_its_start(self):
        campaign = self._campaign(
            status_value=Campaign.Status.SCHEDULED, scheduled_start_at=START_UTC
        )
        new_start = START_UTC + timedelta(days=1)

        response = self.client_alex.patch(
            f"{BASE}{campaign.id}/",
            {"scheduled_start_at": new_start.isoformat()},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        campaign.refresh_from_db()
        self.assertEqual(campaign.scheduled_start_at, new_start)

    def test_a_running_campaign_may_not_move_its_start(self):
        campaign = self._campaign(
            status_value=Campaign.Status.RUNNING, scheduled_start_at=START_UTC
        )

        response = self.client_alex.patch(
            f"{BASE}{campaign.id}/",
            {"scheduled_start_at": (START_UTC + timedelta(days=1)).isoformat()},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        campaign.refresh_from_db()
        self.assertEqual(campaign.scheduled_start_at, START_UTC)

    def test_a_scheduled_campaign_cannot_lose_its_start(self):
        """With no start the sweep never launches it; it would sit SCHEDULED for ever."""
        campaign = self._campaign(
            status_value=Campaign.Status.SCHEDULED, scheduled_start_at=START_UTC
        )
        for payload in (
            {"scheduled_start_at": None},
            {"start_date": None, "start_time": None},
        ):
            with self.subTest(payload=payload):
                response = self.client_alex.patch(
                    f"{BASE}{campaign.id}/", payload, format="json"
                )

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                campaign.refresh_from_db()
                self.assertEqual(campaign.scheduled_start_at, START_UTC)
