"""
BUG-000962 and BUG-000923 — the Event Skill listing and template selection.

* The listing sorts on the three columns it renders.
* The event-template dropdown offers active templates only, and an inactive template
  cannot be linked even by posting its id directly.
"""

from unittest import skipUnless

from django.db import connection
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.models.user_event_skill import UserEventSkill
from core.tests.scheduling import factories as f

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)

SKILLS = "/api/v1/core/event-skills/"
TEMPLATES = "/api/v1/core/event-templates/"


class EventSkillTestCase(APITestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.actor = f.make_user(self.tenant)
        self.client_actor = APIClient()
        self.client_actor.force_authenticate(user=self.actor)

        self.demo = f.make_template(self.tenant, name="30-Min Demo")
        self.intro = f.make_template(self.tenant, name="Intro Call")
        self.host = f.make_user(self.tenant, email="host@example.com")

    def _link(self, user, template, client=None):
        return (client or self.client_actor).post(
            SKILLS,
            {"user": str(user.id), "event_template": str(template.id)},
            format="json",
        )


@REQUIRES_PG
class InactiveTemplateTests(EventSkillTestCase):
    """BUG-000923's other half: the dropdown no longer offers an inactive template,
    but nothing stopped a client posting its id anyway."""

    def test_an_inactive_template_cannot_be_linked(self):
        self.intro.is_active = False
        self.intro.save(update_fields=["is_active"])

        response = self._link(self.host, self.intro)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("event_template", response.data["errors"])

    def test_an_active_template_still_links(self):
        self.assertEqual(self._link(self.host, self.demo).status_code, 201)

    def test_a_host_may_hold_several_skills(self):
        """Multiple templates per host is intended behaviour, not a duplicate."""
        self.assertEqual(self._link(self.host, self.demo).status_code, 201)
        self.assertEqual(self._link(self.host, self.intro).status_code, 201)
        self.assertEqual(
            UserEventSkill.objects.filter(user=self.host).count(), 2
        )

    def test_the_same_pair_twice_is_still_refused(self):
        self._link(self.host, self.demo)
        self.assertEqual(
            self._link(self.host, self.demo).status_code, status.HTTP_400_BAD_REQUEST
        )


@REQUIRES_PG
class EventSkillSortingTests(EventSkillTestCase):
    """BUG-000962."""

    def setUp(self):
        super().setUp()
        # Created in an order matching no sort key, so a pass cannot be the default
        # `-created_at` in disguise.
        self.zara = f.make_user(self.tenant, email="zara@example.com")
        self.zara.first_name, self.zara.last_name = "Zara", "Ahmed"
        self.zara.save(update_fields=["first_name", "last_name"])

        self.ada = f.make_user(self.tenant, email="ada@example.com")
        self.ada.first_name, self.ada.last_name = "ada", "lovelace"
        self.ada.save(update_fields=["first_name", "last_name"])

        self.grace = f.make_user(self.tenant, email="grace@example.com")
        self.grace.first_name, self.grace.last_name = "Grace", "Hopper"
        self.grace.save(update_fields=["first_name", "last_name"])

        self.middle = f.make_template(self.tenant, name="Middle Template")

        UserEventSkill.objects.create(
            tenant=self.tenant, user=self.zara, event_template=self.demo
        )
        UserEventSkill.objects.create(
            tenant=self.tenant, user=self.ada, event_template=self.intro
        )
        UserEventSkill.objects.create(
            tenant=self.tenant,
            user=self.grace,
            event_template=self.middle,
            is_active=False,
        )

    def _rows(self, ordering):
        response = self.client_actor.get(SKILLS, {"ordering": ordering})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data["data"]

    def test_user_name_ascending(self):
        emails = [row["user"]["email"] for row in self._rows("user_name")]
        self.assertEqual(
            emails, ["ada@example.com", "grace@example.com", "zara@example.com"]
        )

    def test_user_name_descending(self):
        emails = [row["user"]["email"] for row in self._rows("-user_name")]
        self.assertEqual(
            emails, ["zara@example.com", "grace@example.com", "ada@example.com"]
        )

    def test_user_name_falls_back_to_email_when_no_name_is_set(self):
        """Matches what the table renders, rather than grouping unnamed users at ''."""
        nameless = f.make_user(self.tenant, email="aaa@example.com")
        UserEventSkill.objects.create(
            tenant=self.tenant, user=nameless, event_template=f.make_template(self.tenant)
        )
        emails = [row["user"]["email"] for row in self._rows("user_name")]
        self.assertEqual(emails[0], "aaa@example.com")

    def test_template_name(self):
        names = [row["event_template"]["name"] for row in self._rows("template_name")]
        self.assertEqual(names, ["30-Min Demo", "Intro Call", "Middle Template"])

    def test_template_name_descending(self):
        names = [row["event_template"]["name"] for row in self._rows("-template_name")]
        self.assertEqual(names, ["Middle Template", "Intro Call", "30-Min Demo"])

    def test_is_active(self):
        flags = [row["is_active"] for row in self._rows("is_active")]
        self.assertEqual(flags[0], False)
        self.assertEqual(flags[1:], [True, True])

    def test_the_existing_relation_paths_still_work(self):
        """The new aliases are additive; a client on the old names must not break."""
        names = [row["event_template"]["name"] for row in self._rows("event_template__name")]
        self.assertEqual(names, ["30-Min Demo", "Intro Call", "Middle Template"])


@REQUIRES_PG
class EventTemplateDropdownTests(EventSkillTestCase):
    """BUG-000923."""

    def setUp(self):
        super().setUp()
        self.intro.is_active = False
        self.intro.save(update_fields=["is_active"])

    def test_dropdown_offers_active_templates_only(self):
        response = self.client_actor.get(f"{TEMPLATES}dropdown/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        labels = [row["label"] for row in response.data["data"]]
        self.assertIn("30-Min Demo", labels)
        self.assertNotIn("Intro Call", labels)

    def test_the_management_list_still_shows_inactive_templates(self):
        """They have to stay visible somewhere, or they can never be reactivated."""
        response = self.client_actor.get(TEMPLATES)
        names = [row["name"] for row in response.data["data"]]
        self.assertIn("Intro Call", names)
