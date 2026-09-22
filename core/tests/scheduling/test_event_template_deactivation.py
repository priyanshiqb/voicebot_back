"""
Deactivating an event type while something still depends on it.

Two things keep a type live: a bot that offers it, and a host who can take it — an active
skill for the type, held by someone with an active availability profile. Deactivating
under either would leave a bot offering meetings that are all refused, or hosts whose
calendars quietly stop producing slots for it, so it is refused. Reactivating is always
allowed, and inactive types stay out of the dropdown.
"""

from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.models.agent import Agent
from core.models.availability_profile import AvailabilityProfile
from core.models.event_template import EventTemplate
from core.models.user_event_skill import UserEventSkill
from core.tests.scheduling import factories as f

TEMPLATES = "/api/v1/core/event-templates/"


class EventTemplateDeactivationTests(APITestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        actor = f.make_user(self.tenant)
        self.client_actor = APIClient()
        self.client_actor.force_authenticate(user=actor)
        self.template = f.make_template(self.tenant, name="30-Min Demo")

    def _patch(self, is_active):
        return self.client_actor.patch(
            f"{TEMPLATES}{self.template.id}/", {"is_active": is_active}, format="json"
        )

    def _bot(self, name, **fields):
        agent = Agent.objects.create(
            tenant=self.tenant, bot_name=name, persona_name="P", **fields
        )
        agent.event_templates.add(self.template)
        return agent

    def _assert_still_active(self):
        self.template.refresh_from_db()
        self.assertTrue(self.template.is_active)

    def test_an_unused_type_may_be_deactivated(self):
        response = self._patch(False)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.template.refresh_from_db()
        self.assertFalse(self.template.is_active)

    def test_a_type_an_active_bot_offers_cannot_be_deactivated(self):
        self._bot("Sales Bot")

        response = self._patch(False)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("is_active", response.data["errors"])
        self.assertIn("Sales Bot", response.data["message"])
        self._assert_still_active()

    def test_an_inactive_or_deleted_bot_does_not_block(self):
        self._bot("Paused Bot", is_active=False)
        self._bot("Old Bot", is_deleted=True)

        self.assertEqual(self._patch(False).status_code, status.HTTP_200_OK)

    def test_a_host_with_an_active_profile_blocks_deactivation(self):
        f.make_host(self.tenant, self.template, email="host@example.com")

        response = self._patch(False)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("host@example.com", response.data["message"])
        self._assert_still_active()

    def test_a_host_whose_skill_is_inactive_does_not_block(self):
        host = f.make_host(self.tenant, self.template)
        UserEventSkill.objects.filter(user=host, event_template=self.template).update(
            is_active=False
        )

        self.assertEqual(self._patch(False).status_code, status.HTTP_200_OK)

    def test_a_host_whose_profile_is_inactive_does_not_block(self):
        host = f.make_host(self.tenant, self.template)
        AvailabilityProfile.objects.filter(user=host).update(is_active=False)

        self.assertEqual(self._patch(False).status_code, status.HTTP_200_OK)

    def test_reactivating_is_always_allowed(self):
        EventTemplate.objects.filter(pk=self.template.pk).update(is_active=False)
        self._bot("Sales Bot")

        response = self._patch(True)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_inactive_types_are_left_out_of_the_dropdown(self):
        retired = f.make_template(self.tenant, name="Retired Call", is_active=False)

        response = self.client_actor.get(f"{TEMPLATES}dropdown/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        values = {str(row["value"]) for row in response.data["data"]}
        self.assertIn(str(self.template.id), values)
        self.assertNotIn(str(retired.id), values)

    def test_an_inactive_type_cannot_be_added_to_a_bot(self):
        """The other way into the same state: attaching a type after deactivating it."""
        EventTemplate.objects.filter(pk=self.template.pk).update(is_active=False)
        agent = Agent.objects.create(
            tenant=self.tenant, bot_name="Sales Bot", persona_name="P"
        )

        response = self.client_actor.patch(
            f"/api/v1/core/agents/{agent.id}/",
            {"add_event_templates": [str(self.template.id)]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("add_event_templates", response.data["errors"])
        self.assertFalse(agent.event_templates.exists())
