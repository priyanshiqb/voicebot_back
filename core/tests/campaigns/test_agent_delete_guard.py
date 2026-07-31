"""
Agent.soft_delete must refuse while a live campaign depends on the bot.

PROTECT on Campaign.agent cannot cover this: agents are *soft*-deleted, so no row is
ever removed and the database constraint never fires. Without the explicit guard, a
campaign mid-run is left pointing at a bot the UI considers gone.

This is a behaviour change to pre-existing code, so the last test here pins the
unaffected path.
"""

from django.core.exceptions import ValidationError
from django.test import TestCase

from core.models.agent import Agent
from core.models.campaign import Campaign
from tenant.models.Tenant import Tenant


class AgentDeleteGuardTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-agent-guard")
        self.agent = Agent.objects.create(
            tenant=self.tenant, bot_name="Sales Bot", persona_name="Persona"
        )

    def _campaign(self, status, name="Campaign"):
        return Campaign.objects.create(
            tenant=self.tenant, agent=self.agent, name=name, status=status
        )

    def test_blocked_by_each_live_status(self):
        for index, status in enumerate(Campaign.LIVE_STATUSES):
            with self.subTest(status=status):
                campaign = self._campaign(status, name=f"Live {index}")
                with self.assertRaises(ValidationError):
                    self.agent.soft_delete()

                self.agent.refresh_from_db()
                self.assertFalse(self.agent.is_deleted)
                campaign.delete()

    def test_allowed_when_campaign_is_draft(self):
        self._campaign(Campaign.Status.DRAFT, name="Draft One")
        self.agent.soft_delete()
        self.agent.refresh_from_db()
        self.assertTrue(self.agent.is_deleted)

    def test_allowed_when_campaigns_are_terminal(self):
        for index, status in enumerate(Campaign.TERMINAL_STATUSES):
            self._campaign(status, name=f"Done {index}")

        self.agent.soft_delete()
        self.agent.refresh_from_db()
        self.assertTrue(self.agent.is_deleted)

    def test_error_names_the_blocking_count(self):
        self._campaign(Campaign.Status.RUNNING, name="Running One")
        self._campaign(Campaign.Status.PAUSED, name="Paused One")

        with self.assertRaises(ValidationError) as ctx:
            self.agent.soft_delete()
        self.assertIn("2", str(ctx.exception))

    def test_soft_deleted_campaign_does_not_block(self):
        """A cancelled-then-deleted campaign is not a reason to keep a bot alive."""
        campaign = self._campaign(Campaign.Status.RUNNING, name="Gone")
        campaign.soft_delete()

        self.agent.soft_delete()
        self.agent.refresh_from_db()
        self.assertTrue(self.agent.is_deleted)

    def test_unrelated_agent_is_unaffected(self):
        self._campaign(Campaign.Status.RUNNING, name="Busy")
        spare = Agent.objects.create(
            tenant=self.tenant, bot_name="Spare Bot", persona_name="Persona"
        )

        spare.soft_delete()
        spare.refresh_from_db()
        self.assertTrue(spare.is_deleted)

    def test_existing_delete_behaviour_is_preserved(self):
        """Regression: the pre-existing bot_name rename must still happen."""
        agent = Agent.objects.create(
            tenant=self.tenant, bot_name="Renamed Bot", persona_name="Persona"
        )
        agent.soft_delete()
        agent.refresh_from_db()

        self.assertTrue(agent.is_deleted)
        self.assertIsNotNone(agent.deleted_at)
        self.assertIn("-deleted-", agent.bot_name)
        # The name is freed for reuse.
        Agent.objects.create(
            tenant=self.tenant, bot_name="Renamed Bot", persona_name="Persona"
        )
