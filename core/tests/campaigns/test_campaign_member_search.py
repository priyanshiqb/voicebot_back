"""
Campaign member listing — `?search=` matches the member's name as well as the number.
"""

from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.models.agent import Agent
from core.models.campaign import Campaign
from core.models.campaign_member import CampaignMember
from core.models.contact import Contact
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models import CustomUser as User

BASE = "/api/v1/core/campaigns/"


class CampaignMemberSearchTests(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-member-search")
        user = User.objects.create_user(
            username="alex", email="alex@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=user, tenant=self.tenant)
        self.client_alex = APIClient()
        self.client_alex.force_authenticate(user=user)

        agent = Agent.objects.create(
            tenant=self.tenant, bot_name="Sales Bot", persona_name="P"
        )
        self.campaign = Campaign.objects.create(
            tenant=self.tenant, name="Q1 Leads", agent=agent
        )
        self.ada = self._member("Ada", "Lovelace", "+14155550001")
        self.grace = self._member("Grace", "Hopper", "+14155550002")

    def _member(self, first, last, phone):
        contact = Contact.objects.create(
            tenant=self.tenant, first_name=first, last_name=last, phone_number=phone
        )
        return CampaignMember.objects.create(
            tenant=self.tenant,
            campaign=self.campaign,
            contact=contact,
            phone_e164=contact.phone_e164,
        )

    def _found(self, search):
        response = self.client_alex.get(
            f"{BASE}{self.campaign.id}/members/", {"search": search}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {row["id"] for row in response.data["data"]}

    def test_first_name(self):
        self.assertEqual(self._found("ada"), {str(self.ada.id)})

    def test_last_name_in_any_casing(self):
        self.assertEqual(self._found("HOPPER"), {str(self.grace.id)})

    def test_first_and_part_of_last(self):
        self.assertEqual(self._found("grace hop"), {str(self.grace.id)})

    def test_the_number_still_matches(self):
        self.assertEqual(self._found("5550001"), {str(self.ada.id)})

    def test_no_match(self):
        self.assertEqual(self._found("nobody"), set())
