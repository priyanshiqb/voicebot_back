"""
Deleting an agent that a live campaign depends on.

`Agent.soft_delete` refuses with a written explanation, but it raises Django's
`ValidationError` — which DRF's exception handler does not recognise. Before the
translation added to `utils.exception`, that fell through to the catch-all and
the caller got a 500 "Something went wrong", discarding the message entirely and
reading as a server fault rather than a deliberate refusal.

The `PROTECT` foreign key cannot cover this on its own: agents are soft-deleted,
so the database constraint never fires and a running campaign would be left
pointing at a deleted bot mid-run.
"""

from unittest import skipUnless

from django.db import connection
from rest_framework.test import APIClient, APITestCase

from core.models.agent import Agent
from core.models.campaign import Campaign
from core.tests.scheduling import factories as f

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Campaign models require PostgreSQL."
)

AGENTS_URL = "/api/v1/core/agents/"


@REQUIRES_PG
class AgentDeleteGuardTests(APITestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.actor = f.make_user(self.tenant)
        self.agent = Agent.objects.create(
            tenant=self.tenant, bot_name="Booker", persona_name="Assistant"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.actor)

    def _campaign(self, status_value):
        return Campaign.objects.create(
            tenant=self.tenant,
            name=f"Campaign {status_value}",
            agent=self.agent,
            status=status_value,
        )

    def test_delete_without_campaigns_succeeds(self):
        response = self.client.delete(f"{AGENTS_URL}{self.agent.id}/")

        self.assertEqual(response.status_code, 200, response.data)
        self.agent.refresh_from_db()
        self.assertTrue(self.agent.is_deleted)

    def test_live_campaign_blocks_delete_with_400_not_500(self):
        self._campaign(Campaign.LIVE_STATUSES[0])

        response = self.client.delete(f"{AGENTS_URL}{self.agent.id}/")

        self.assertEqual(
            response.status_code,
            400,
            f"expected a refusal, got {response.status_code}: {response.data}",
        )
        self.assertIn("campaign", str(response.data).lower())
        self.agent.refresh_from_db()
        self.assertFalse(self.agent.is_deleted, "the agent was deleted anyway")

    def test_the_refusal_explains_itself(self):
        """The whole point of the guard is the message — a bare 400 is not enough
        for someone to work out what to cancel."""
        self._campaign(Campaign.LIVE_STATUSES[0])

        response = self.client.delete(f"{AGENTS_URL}{self.agent.id}/")

        body = str(response.data)
        self.assertIn("Cancel them before deleting", body)
        self.assertNotIn("Something went wrong", body)

    def test_deleting_frees_the_bot_name_for_reuse(self):
        """soft_delete suffixes the name so the unique-per-tenant constraint does
        not block someone recreating a bot with the same name."""
        self.client.delete(f"{AGENTS_URL}{self.agent.id}/")

        response = self.client.post(
            AGENTS_URL,
            {
                "bot_name": "Booker",
                "persona_name": "Assistant",
                "languages": ["en-IN"],
                "tone": Agent.ToneChoices.PROFESSIONAL,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
