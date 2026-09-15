"""
Campaign member listing — call duration.

A member has no duration of its own; it is the length of the member's latest call, the
same call `analytics_id` opens. These pin that "latest" is what every part reads (the
row, `min_duration` / `max_duration`, and `ordering=duration_seconds`), what happens to
members with no call, and that malformed bounds are refused rather than matching nobody.
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
UTC = ZoneInfo("UTC")
T0 = datetime(2027, 2, 15, 9, 0, tzinfo=UTC)


class CampaignMemberDurationTests(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-member-duration")
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

        self.short = self._member("Short", "+14155550001", CampaignMember.Status.COMPLETED)
        self._call(self.short, "call-short", 20, T0)

        # Called twice: a 10-second first attempt, then a 400-second latest call. Any
        # test where the 10 seconds would decide the answer proves "latest", not "any".
        self.long = self._member("Long", "+14155550002", CampaignMember.Status.COMPLETED)
        self._call(self.long, "call-long-first", 10, T0)
        self._call(self.long, "call-long-latest", 400, T0 + timedelta(hours=1))

        self.never = self._member("Never", "+14155550003", CampaignMember.Status.PENDING)

    def _member(self, first_name, phone, member_status):
        contact = Contact.objects.create(
            tenant=self.tenant, first_name=first_name, phone_number=phone
        )
        return CampaignMember.objects.create(
            tenant=self.tenant,
            campaign=self.campaign,
            contact=contact,
            phone_e164=contact.phone_e164,
            status=member_status,
        )

    def _call(self, member, call_id, seconds, logged_at):
        log = AgentCallLog.objects.create(
            tenant=self.tenant,
            agent=self.agent,
            provider_call_id=call_id,
            caller_number="+18005550100",
            callee_number=member.phone_e164,
            direction=AgentCallLog.CallDirection.OUTBOUND,
            duration_seconds=seconds,
            campaign_member=member,
        )
        # created_at is auto_now_add, so set it after the fact to control "latest".
        AgentCallLog.objects.filter(pk=log.pk).update(created_at=logged_at)

    def _get(self, **params):
        return self.client_alex.get(f"{BASE}{self.campaign.id}/members/", params)

    def _durations(self, **params):
        response = self._get(**params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return {row["id"]: row["duration_seconds"] for row in response.data["data"]}

    def _ids(self, *members):
        return {str(member.id) for member in members}

    def test_rows_carry_the_latest_calls_duration(self):
        durations = self._durations()

        self.assertEqual(durations[str(self.short.id)], 20)
        self.assertEqual(durations[str(self.long.id)], 400)
        self.assertIsNone(durations[str(self.never.id)])

    def test_min_duration_is_inclusive(self):
        self.assertEqual(
            set(self._durations(min_duration=20)), self._ids(self.short, self.long)
        )

    def test_max_duration_is_inclusive(self):
        self.assertEqual(set(self._durations(max_duration=20)), self._ids(self.short))

    def test_both_bounds_together(self):
        self.assertEqual(
            set(self._durations(min_duration=21, max_duration=400)), self._ids(self.long)
        )

    def test_an_earlier_call_does_not_count(self):
        """Long's first attempt was 10 seconds, but its latest call is what is filtered."""
        self.assertEqual(set(self._durations(max_duration=15)), set())

    def test_without_a_duration_filter_never_called_members_stay(self):
        self.assertIn(str(self.never.id), self._durations())

    def test_either_bound_leaves_never_called_members_out(self):
        self.assertNotIn(str(self.never.id), self._durations(min_duration=0))
        self.assertNotIn(str(self.never.id), self._durations(max_duration=100000))

    def test_an_empty_parameter_counts_as_not_sent(self):
        self.assertEqual(len(self._durations(min_duration="", max_duration="")), 3)

    def test_malformed_bounds_are_refused(self):
        cases = [
            ({"min_duration": "abc"}, "min_duration"),
            ({"max_duration": "-1"}, "max_duration"),
            ({"min_duration": "12.5"}, "min_duration"),
            ({"min_duration": "50", "max_duration": "10"}, "max_duration"),
        ]
        for params, field in cases:
            with self.subTest(params=params):
                response = self._get(**params)

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(field, response.data["errors"])

    def test_sort_by_duration(self):
        response = self._get(ordering="duration_seconds")
        ascending = [row["duration_seconds"] for row in response.data["data"]]
        response = self._get(ordering="-duration_seconds")
        descending = [row["duration_seconds"] for row in response.data["data"]]

        # Postgres places nulls after every value ascending and before them descending,
        # the same as ordering=last_call_at already does.
        self.assertEqual(ascending, [20, 400, None])
        self.assertEqual(descending, [None, 400, 20])

    def test_duration_composes_with_the_status_filter(self):
        self.assertEqual(
            set(self._durations(status="COMPLETED", min_duration=100)),
            self._ids(self.long),
        )
