"""
Event skill listing — the added sort keys.

Beyond the table's own columns, a skill row can be sorted by its host's email and by its
template's settings. These pin the table-style aliases, which resolve through
`ordering_field_map` rather than naming a queryset path directly.
"""

from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.models.user_event_skill import UserEventSkill
from core.tests.scheduling import factories as f

SKILLS = "/api/v1/core/event-skills/"


class EventSkillOrderingTests(APITestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        actor = f.make_user(self.tenant)
        self.client_actor = APIClient()
        self.client_actor.force_authenticate(user=actor)

        short = f.make_template(
            self.tenant, name="Short", duration_minutes=15, slot_granularity_minutes=15
        )
        long = f.make_template(self.tenant, name="Long", duration_minutes=60)
        retired = f.make_template(
            self.tenant, name="Retired", duration_minutes=30, is_active=False
        )

        self.skills = {
            "b": self._skill("b@example.com", short),
            "c": self._skill("c@example.com", long),
            "a": self._skill("a@example.com", retired),
        }

    def _skill(self, email, template):
        user = f.make_user(self.tenant, email=email)
        return UserEventSkill.objects.create(
            tenant=self.tenant, user=user, event_template=template
        )

    def _order(self, ordering):
        response = self.client_actor.get(SKILLS, {"ordering": ordering})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return [row["id"] for row in response.data["data"]]

    def _ids(self, *keys):
        return [str(self.skills[key].id) for key in keys]

    def test_user_email(self):
        self.assertEqual(self._order("user_email"), self._ids("a", "b", "c"))
        self.assertEqual(self._order("-user_email"), self._ids("c", "b", "a"))

    def test_template_duration(self):
        self.assertEqual(self._order("template_duration"), self._ids("b", "a", "c"))

    def test_template_is_active(self):
        """The retired template's skill sorts first ascending: False before True."""
        self.assertEqual(self._order("template_is_active")[0], str(self.skills["a"].id))
