"""
Campaign member listing — `analytics_id`.

Each member row carries the id of its newest call log, so the member table can open the
call's analytics (transcript, summary, recording) without searching call logs by number.
Which call that is, and when there is none, are what these pin down.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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

BASE = "/api/v1/core/campaigns/"
CALL_LOGS = "/api/v1/core/agent-call-logs/"
UTC = ZoneInfo("UTC")


class CampaignMemberAnalyticsIdTests(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-member-analytics")
        self.user = User.objects.create_user(
            username="alex", email="alex@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)
        self.client_alex = APIClient()
        self.client_alex.force_authenticate(user=self.user)

        self.agent = Agent.objects.create(
            tenant=self.tenant, bot_name="Sales Bot", persona_name="Persona"
        )
        self.campaign = Campaign.objects.create(
            tenant=self.tenant, name="Q1 Leads", agent=self.agent
        )
        self.ada = self._member("Ada", "+14155550001")
        self.grace = self._member("Grace", "+14155550002")

    def _member(self, first_name, phone):
        contact = Contact.objects.create(
            tenant=self.tenant, first_name=first_name, phone_number=phone
        )
        return CampaignMember.objects.create(
            tenant=self.tenant,
            campaign=self.campaign,
            contact=contact,
            phone_e164=contact.phone_e164,
            status=CampaignMember.Status.COMPLETED,
        )

    def _call(self, member, call_id, logged_at=None):
        log = AgentCallLog.objects.create(
            tenant=self.tenant,
            agent=self.agent,
            provider_call_id=call_id,
            caller_number="+18005550100",
            callee_number=member.phone_e164,
            direction=AgentCallLog.CallDirection.OUTBOUND,
            duration_seconds=60,
            campaign_member=member,
        )
        if logged_at is not None:
            # created_at is auto_now_add, so set it after the fact to control "newest".
            AgentCallLog.objects.filter(pk=log.pk).update(created_at=logged_at)
        return log

    def _analytics_ids(self):
        response = self.client_alex.get(f"{BASE}{self.campaign.id}/members/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {row["id"]: row["analytics_id"] for row in response.data["data"]}

    def test_member_never_called_has_none(self):
        self.assertIsNone(self._analytics_ids()[str(self.ada.id)])

    def test_it_is_the_linked_call_log(self):
        log = self._call(self.ada, "call-ada-1")

        self.assertEqual(self._analytics_ids()[str(self.ada.id)], str(log.id))

    def test_member_called_twice_points_at_the_newest_call(self):
        base = datetime(2027, 2, 15, 9, 0, tzinfo=UTC)
        self._call(self.ada, "call-ada-first", logged_at=base)
        newest = self._call(self.ada, "call-ada-second", logged_at=base + timedelta(hours=2))

        self.assertEqual(self._analytics_ids()[str(self.ada.id)], str(newest.id))

    def test_a_soft_deleted_call_log_is_skipped(self):
        """Its detail endpoint 404s, so pointing at it would be a dead link."""
        base = datetime(2027, 2, 15, 9, 0, tzinfo=UTC)
        earlier = self._call(self.ada, "call-ada-first", logged_at=base)
        deleted = self._call(self.ada, "call-ada-second", logged_at=base + timedelta(hours=2))
        AgentCallLog.objects.filter(pk=deleted.pk).update(is_deleted=True)

        self.assertEqual(self._analytics_ids()[str(self.ada.id)], str(earlier.id))

    def test_each_member_gets_its_own_call(self):
        ada_log = self._call(self.ada, "call-ada-1")
        grace_log = self._call(self.grace, "call-grace-1")

        ids = self._analytics_ids()
        self.assertEqual(ids[str(self.ada.id)], str(ada_log.id))
        self.assertEqual(ids[str(self.grace.id)], str(grace_log.id))

    def test_the_id_opens_the_call_log_detail(self):
        """The point of the field: it is exactly what the call analytics endpoint takes."""
        self._call(self.ada, "call-ada-1")
        analytics_id = self._analytics_ids()[str(self.ada.id)]

        response = self.client_alex.get(f"{CALL_LOGS}{analytics_id}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["provider_call_id"], "call-ada-1")
