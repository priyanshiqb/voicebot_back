"""
BUG-001230 — every sortable column on the contact table actually sorts.

`full_name` is the one that needs real machinery: it is a model property spanning two
columns, so it can only be ordered on through an annotation, and the annotation cannot
be called `full_name` (DRF assigns annotations onto the instance, and a read-only
property of that name makes the assignment raise). These tests fail if either half of
that arrangement is dropped.
"""

from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.models.contact import Contact
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models import CustomUser as User

BASE = "/api/v1/core/contacts/"


class ContactSortingTests(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-sort")
        self.user = User.objects.create_user(
            username="alex", email="alex@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)
        self.client_alex = APIClient()
        self.client_alex.force_authenticate(user=self.user)

        # Deliberately mixed case, and created in an order that matches no sort key,
        # so a passing assertion cannot be the default `-created_at` in disguise.
        self.rows = [
            ("Zara", "Ahmed", "+14155550003", "zara@example.com", "Cyberdyne", "GRANTED"),
            ("ada", "lovelace", "+14155550001", "ada@example.com", "Analytical", "DENIED"),
            ("Grace", "Hopper", "+14155550002", "grace@example.com", "Bell Labs", "UNKNOWN"),
        ]
        for first, last, phone, email, company, consent in self.rows:
            Contact.objects.create(
                tenant=self.tenant,
                first_name=first,
                last_name=last,
                phone_number=phone,
                email_address=email,
                company=company,
                consent_status=consent,
            )

    def _names(self, ordering):
        response = self.client_alex.get(BASE, {"ordering": ordering})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [row["full_name"] for row in response.data["data"]]

    def _values(self, ordering, key):
        response = self.client_alex.get(BASE, {"ordering": ordering})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [row[key] for row in response.data["data"]]

    def test_full_name_ascending(self):
        self.assertEqual(
            self._names("full_name"), ["ada lovelace", "Grace Hopper", "Zara Ahmed"]
        )

    def test_full_name_descending(self):
        self.assertEqual(
            self._names("-full_name"), ["Zara Ahmed", "Grace Hopper", "ada lovelace"]
        )

    def test_full_name_sort_is_case_insensitive(self):
        """
        Postgres orders by byte value, which files every lowercase name after every
        uppercase one — 'ada' would sort last. The annotation lowercases so the column
        sorts the way the list reads.
        """
        self.assertEqual(self._names("full_name")[0], "ada lovelace")

    def test_full_name_still_serializes_after_being_ordered_on(self):
        """
        The annotation must not be named `full_name`. If it were, DRF would try to
        assign it over the model property and raise AttributeError instead of listing.
        """
        response = self.client_alex.get(BASE, {"ordering": "full_name"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"][0]["full_name"], "ada lovelace")

    def test_phone_e164(self):
        self.assertEqual(
            self._values("phone_e164", "phone_e164"),
            ["+14155550001", "+14155550002", "+14155550003"],
        )

    def test_email_address(self):
        self.assertEqual(
            self._values("email_address", "email_address"),
            ["ada@example.com", "grace@example.com", "zara@example.com"],
        )

    def test_company(self):
        self.assertEqual(
            self._values("company", "company"),
            ["Analytical", "Bell Labs", "Cyberdyne"],
        )

    def test_consent_status(self):
        self.assertEqual(
            self._values("consent_status", "consent_status"),
            ["DENIED", "GRANTED", "UNKNOWN"],
        )

    def test_multiple_keys(self):
        Contact.objects.create(
            tenant=self.tenant,
            first_name="Alan",
            last_name="Turing",
            phone_number="+14155550004",
            company="Analytical",
        )
        response = self.client_alex.get(BASE, {"ordering": "company,-full_name"})
        rows = [(row["company"], row["full_name"]) for row in response.data["data"]]
        # Both are at "Analytical", so the second key decides: descending on the
        # lowercased name puts "alan turing" ahead of "ada lovelace".
        self.assertEqual(rows[0], ("Analytical", "Alan Turing"))
        self.assertEqual(rows[1], ("Analytical", "ada lovelace"))
        self.assertEqual(rows[2][0], "Bell Labs")

    def test_an_unknown_key_falls_back_to_the_default_rather_than_erroring(self):
        response = self.client_alex.get(BASE, {"ordering": "not_a_column"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 3)
