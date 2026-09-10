"""
Campaign member listing — sorting.

The members action builds its own queryset and never runs `filter_queryset`, so the
viewset's `ordering_fields` (which describe campaigns) never reached it and `?ordering=`
was ignored outright. These cover the four columns the member table renders, plus the
tie-break that keeps paging stable.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
UTC = ZoneInfo("UTC")


class CampaignMemberSortingTests(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-member-sort")
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

        # Mixed case and an insertion order matching no sort key, so a pass cannot be
        # the default ordering in disguise.
        rows = [
            ("Zara", "Ahmed", "+14155550003", 5, "COMPLETED", 3),
            ("ada", "lovelace", "+14155550001", 0, "PENDING", 1),
            ("Grace", "Hopper", "+14155550002", 2, "FAILED", 2),
        ]
        base = datetime(2027, 2, 15, 4, 0, tzinfo=UTC)
        for first, last, phone, attempts, member_status, day in rows:
            contact = Contact.objects.create(
                tenant=self.tenant,
                first_name=first,
                last_name=last,
                phone_number=phone,
            )
            CampaignMember.objects.create(
                tenant=self.tenant,
                campaign=self.campaign,
                contact=contact,
                phone_e164=contact.phone_e164,
                status=member_status,
                attempt_count=attempts,
                last_call_at=base + timedelta(days=day),
            )

    def _rows(self, ordering=None):
        params = {"ordering": ordering} if ordering else {}
        response = self.client_alex.get(f"{BASE}{self.campaign.id}/members/", params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data["data"]

    def _values(self, ordering, key):
        return [row[key] for row in self._rows(ordering)]

    def test_full_name_ascending(self):
        self.assertEqual(
            self._values("full_name", "full_name"),
            ["ada lovelace", "Grace Hopper", "Zara Ahmed"],
        )

    def test_full_name_descending(self):
        self.assertEqual(
            self._values("-full_name", "full_name"),
            ["Zara Ahmed", "Grace Hopper", "ada lovelace"],
        )

    def test_attempt_count(self):
        self.assertEqual(self._values("attempt_count", "attempt_count"), [0, 2, 5])
        self.assertEqual(self._values("-attempt_count", "attempt_count"), [5, 2, 0])

    def test_status(self):
        self.assertEqual(
            self._values("status", "status"), ["COMPLETED", "FAILED", "PENDING"]
        )

    def test_last_call_at(self):
        values = self._values("-last_call_at", "last_call_at")
        self.assertEqual(values, sorted(values, reverse=True))

    def test_default_is_oldest_first(self):
        """Unchanged from before sorting existed — the model's own Meta ordering."""
        self.assertEqual(
            self._values(None, "full_name"),
            ["Zara Ahmed", "ada lovelace", "Grace Hopper"],
        )

    def test_paging_a_wholly_tied_sort_covers_every_row_exactly_once(self):
        """
        Members materialised in one transaction share a status and an attempt count,
        so sorting on either leaves the whole audience in one tie. SQL makes no promise
        about the order of tied rows between queries, so without a tiebreaker page 2
        can repeat a row from page 1 and drop another.
        """
        CampaignMember.objects.all().update(attempt_count=0)

        seen = []
        for page in (1, 2):
            response = self.client_alex.get(
                f"{BASE}{self.campaign.id}/members/",
                {"ordering": "attempt_count", "page_size": 2, "page": page},
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            seen.extend(row["id"] for row in response.data["data"])

        self.assertEqual(len(seen), 3)
        self.assertEqual(len(set(seen)), 3, "a row appeared on two pages")

    def test_the_tiebreaker_does_not_override_the_requested_sort(self):
        """It settles ties; it must not reorder rows the client did distinguish."""
        self.assertEqual(self._values("-attempt_count", "attempt_count"), [5, 2, 0])

    def test_an_unknown_key_falls_back_to_the_default_rather_than_erroring(self):
        response = self.client_alex.get(
            f"{BASE}{self.campaign.id}/members/", {"ordering": "not_a_column"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 3)

    def test_sorting_composes_with_the_status_filter(self):
        rows = self.client_alex.get(
            f"{BASE}{self.campaign.id}/members/",
            {"status": "PENDING", "ordering": "full_name"},
        ).data["data"]
        self.assertEqual([row["full_name"] for row in rows], ["ada lovelace"])
