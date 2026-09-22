"""
Agent call log listing — filtering by campaign.

`?campaign=` takes one or several campaign ids and matches a call through its campaign
member, or through the runner's free-text `campaign_id` tag when that holds the id: a call
whose member link was never made still belongs to its campaign. `?campaign_id=` is the tag
itself, exact, and now takes several values too.
"""

from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.models.agent import Agent
from core.models.agent_call_log import AgentCallLog
from core.models.campaign import Campaign
from core.models.campaign_member import CampaignMember
from core.models.contact import Contact
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models import CustomUser as User

URL = "/api/v1/core/agent-call-logs/"


class CallLogCampaignFilterTests(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-log-campaign")
        user = User.objects.create_user(
            username="alex", email="alex@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=user, tenant=self.tenant)
        self.client_alex = APIClient()
        self.client_alex.force_authenticate(user=user)

        self.agent = Agent.objects.create(
            tenant=self.tenant, bot_name="Sales Bot", persona_name="P"
        )
        self.spring = self._campaign("Spring")
        self.summer = self._campaign("Summer")
        self.autumn = self._campaign("Autumn")

        self._call("call-spring", member=self._member(self.spring, "+14155550001"))
        self._call("call-summer", member=self._member(self.summer, "+14155550002"))
        # Tagged with its campaign's id by the runner, but the member link never made.
        self._call("call-autumn-tag-only", tag=str(self.autumn.id))
        self._call("call-inbound")

    def _campaign(self, name):
        return Campaign.objects.create(
            tenant=self.tenant,
            agent=self.agent,
            name=name,
            status=Campaign.Status.RUNNING,
        )

    def _member(self, campaign, phone):
        contact = Contact.objects.create(
            tenant=self.tenant, first_name=campaign.name, phone_number=phone
        )
        return CampaignMember.objects.create(
            tenant=self.tenant,
            campaign=campaign,
            contact=contact,
            phone_e164=contact.phone_e164,
        )

    def _call(self, call_id, member=None, tag=None):
        AgentCallLog.objects.create(
            tenant=self.tenant,
            agent=self.agent,
            provider_call_id=call_id,
            caller_number="+18005550100",
            callee_number="+14155559999",
            direction=AgentCallLog.CallDirection.OUTBOUND,
            campaign_member=member,
            campaign_id=tag,
        )

    def _call_ids(self, **params):
        response = self.client_alex.get(URL, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return {row["provider_call_id"] for row in response.data["data"]}

    def test_one_campaign(self):
        self.assertEqual(self._call_ids(campaign=str(self.spring.id)), {"call-spring"})

    def test_several_campaigns_comma_separated(self):
        self.assertEqual(
            self._call_ids(campaign=f"{self.spring.id},{self.summer.id}"),
            {"call-spring", "call-summer"},
        )

    def test_a_call_tagged_with_the_campaign_id_counts_without_a_member_link(self):
        self.assertEqual(
            self._call_ids(campaign=str(self.autumn.id)), {"call-autumn-tag-only"}
        )

    def test_a_malformed_campaign_id_is_refused(self):
        response = self.client_alex.get(URL, {"campaign": "not-a-uuid"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_another_tenants_campaign_matches_nothing(self):
        other_tenant = Tenant.objects.create(name="Other", subdomain="other-log-campaign")
        theirs = Campaign.objects.create(
            tenant=other_tenant,
            agent=Agent.objects.create(
                tenant=other_tenant, bot_name="Other Bot", persona_name="P"
            ),
            name="Theirs",
        )

        self.assertEqual(self._call_ids(campaign=str(theirs.id)), set())

    def test_the_free_text_tag_takes_several_values(self):
        self._call("call-tag-a", tag="q1-leads")
        self._call("call-tag-b", tag="q2-leads")

        self.assertEqual(
            self._call_ids(campaign_id="q1-leads,q2-leads"), {"call-tag-a", "call-tag-b"}
        )
