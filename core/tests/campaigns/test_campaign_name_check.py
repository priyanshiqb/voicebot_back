"""
`POST /campaigns/check-name/` — is a campaign name free in this organisation?

It must give the answer create and update would: unique per tenant, ignoring case, and
deleted campaigns release their name. Both go through `Campaign.name_in_use`, so these
also pin that the two cannot drift apart.
"""

from rest_framework import status

from core.models.campaign import Campaign
from core.tests.campaigns.test_campaign_api import BASE, CampaignAPITestCase

URL = f"{BASE}check-name/"


class CampaignNameCheckTests(CampaignAPITestCase):
    def _is_unique(self, client=None, **payload):
        response = (client or self.client_alex).post(URL, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data["data"]["is_unique"]

    def test_a_free_name_is_available(self):
        self.assertTrue(self._is_unique(name="Q3 Leads"))

    def test_a_taken_name_is_unavailable_in_any_casing(self):
        self._campaign(name="Q3 Leads")

        self.assertFalse(self._is_unique(name="Q3 Leads"))
        self.assertFalse(self._is_unique(name="q3 leads"))

    def test_a_campaign_keeps_its_own_name_when_renaming(self):
        campaign = self._campaign(name="Q3 Leads")

        self.assertTrue(self._is_unique(name="q3 leads", exclude_id=str(campaign.id)))

    def test_another_tenants_campaign_does_not_hold_the_name(self):
        Campaign.objects.create(
            tenant=self.other_tenant, agent=self.other_agent, name="Q3 Leads"
        )

        self.assertTrue(self._is_unique(name="Q3 Leads"))

    def test_a_deleted_campaign_releases_its_name(self):
        campaign = self._campaign(name="Q3 Leads")
        Campaign.objects.filter(pk=campaign.pk).update(is_deleted=True)

        self.assertTrue(self._is_unique(name="Q3 Leads"))

    def test_the_answer_matches_what_create_does(self):
        self._campaign(name="Q3 Leads")

        self.assertFalse(self._is_unique(name="q3 LEADS"))
        response = self.client_alex.post(
            BASE, self._payload(name="q3 LEADS"), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("name", response.data["errors"])

    def test_a_name_is_required(self):
        response = self.client_alex.post(URL, {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("name", response.data["errors"])
