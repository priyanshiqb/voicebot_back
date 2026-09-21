"""
Runner-facing campaign completion.

`POST /campaign-runner/{id}/complete/` is the only thing in this service that moves a
campaign to COMPLETED, so what it accepts and refuses decides whether a finished
campaign can ever look finished. Two properties carry the weight: it is safe to retry,
and it cannot overwrite a decision an operator already made.
"""

from datetime import timedelta

from django.utils import timezone
from rest_framework import status

from core.models.agent import Agent
from core.models.campaign import Campaign
from core.models.campaign_member import CampaignMember
from core.tests.campaigns.test_campaign_runner_api import BASE, RunnerTestCase
from tenant.models.Tenant import Tenant


class RunnerCompleteTests(RunnerTestCase):
    def _complete(self, payload=None, client=None, campaign=None):
        campaign = campaign or self.campaign
        return (client or self.client_runner).post(
            f"{BASE}{campaign.id}/complete/", payload or {}, format="json"
        )

    def test_a_running_campaign_is_completed(self):
        response = self._complete()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(response.data["data"]["already_completed"])

        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.COMPLETED)
        self.assertIsNotNone(self.campaign.completed_at)

    def test_handing_off_may_also_be_completed(self):
        """A short campaign can finish before the handoff status catches up."""
        Campaign.objects.filter(pk=self.campaign.pk).update(
            status=Campaign.Status.HANDING_OFF
        )

        self.assertEqual(self._complete().status_code, status.HTTP_200_OK)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.COMPLETED)

    def test_retrying_is_a_no_op_rather_than_an_error(self):
        """A redelivered callback must not move completed_at or fail the runner."""
        first = self._complete()
        completed_at = first.data["data"]["completed_at"]

        second = self._complete()

        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertTrue(second.data["data"]["already_completed"])
        self.assertEqual(second.data["data"]["completed_at"], completed_at)

    def test_statuses_the_runner_may_not_complete(self):
        refused = [
            Campaign.Status.DRAFT,
            Campaign.Status.SCHEDULED,
            Campaign.Status.MATERIALIZING,
            Campaign.Status.PAUSED,
            Campaign.Status.CANCELLED,
            Campaign.Status.FAILED,
        ]
        for campaign_status in refused:
            with self.subTest(status=campaign_status):
                Campaign.objects.filter(pk=self.campaign.pk).update(
                    status=campaign_status, completed_at=None
                )

                response = self._complete()

                self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
                self.campaign.refresh_from_db()
                self.assertEqual(self.campaign.status, campaign_status)
                self.assertIsNone(self.campaign.completed_at)

    def test_a_human_token_is_refused(self):
        response = self._complete(client=self.client_human)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.RUNNING)

    def test_another_tenants_campaign_is_not_found(self):
        other_tenant = Tenant.objects.create(name="Other", subdomain="other-runner")
        other_campaign = Campaign.objects.create(
            tenant=other_tenant,
            agent=Agent.objects.create(
                tenant=other_tenant, bot_name="Other Bot", persona_name="P"
            ),
            name="Other Campaign",
            status=Campaign.Status.RUNNING,
        )

        response = self._complete(campaign=other_campaign)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        other_campaign.refresh_from_db()
        self.assertEqual(other_campaign.status, Campaign.Status.RUNNING)

    def test_a_late_callback_may_supply_when_it_finished(self):
        finished_at = timezone.now() - timedelta(hours=2)

        response = self._complete({"completed_at": finished_at.isoformat()})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.completed_at, finished_at)

    def test_a_completed_at_in_the_future_is_refused(self):
        response = self._complete(
            {"completed_at": (timezone.now() + timedelta(days=1)).isoformat()}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("completed_at", response.data["errors"])
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.RUNNING)

    def test_unfinished_members_are_reported_and_left_alone(self):
        """A campaign declared finished with work outstanding must say so."""
        response = self._complete()

        self.assertEqual(response.data["data"]["members_left_open"], 1)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, CampaignMember.Status.PENDING)

    def test_counters_are_settled_on_the_way_out(self):
        CampaignMember.objects.filter(pk=self.pending.pk).update(
            status=CampaignMember.Status.COMPLETED
        )

        data = self._complete().data["data"]

        self.assertEqual(data["completed_count"], 1)
        self.assertEqual(data["members_left_open"], 0)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.completed_count, 1)
