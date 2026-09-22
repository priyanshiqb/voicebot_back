"""
Contact listing — sorting by `name`, `phone_number` and `is_active`.

The names the contact table's columns use. `?ordering=` drops a key it does not know
without an error, so these were accepted, ignored, and the list came back in the default
order — sorting looked broken rather than failing.
"""

from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.models.contact import Contact
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models import CustomUser as User

URL = "/api/v1/core/contacts/"


class ContactSortingKeyTests(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Acme", subdomain="acme-contact-sort-keys"
        )
        user = User.objects.create_user(
            username="alex", email="alex@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=user, tenant=self.tenant)
        self.client_alex = APIClient()
        self.client_alex.force_authenticate(user=user)

        # Insertion order matches no sort key, so a pass cannot be the default order.
        self.grace = self._contact("Grace", "Hopper", "+14155550003", is_active=True)
        self.ada = self._contact("ada", "lovelace", "+14155550001", is_active=False)
        self.zara = self._contact("Zara", "Ahmed", "+14155550002", is_active=True)

    def _contact(self, first, last, phone, is_active):
        return Contact.objects.create(
            tenant=self.tenant,
            first_name=first,
            last_name=last,
            phone_number=phone,
            is_active=is_active,
        )

    def _ids(self, ordering):
        response = self.client_alex.get(URL, {"ordering": ordering})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return [row["id"] for row in response.data["data"]]

    def _expected(self, *contacts):
        return [str(contact.id) for contact in contacts]

    def test_name(self):
        self.assertEqual(self._ids("name"), self._expected(self.ada, self.grace, self.zara))
        self.assertEqual(self._ids("-name"), self._expected(self.zara, self.grace, self.ada))

    def test_phone_number(self):
        self.assertEqual(
            self._ids("phone_number"), self._expected(self.ada, self.zara, self.grace)
        )
        self.assertEqual(
            self._ids("-phone_number"), self._expected(self.grace, self.zara, self.ada)
        )

    def test_is_active(self):
        """Inactive sorts before active ascending, after it descending."""
        self.assertEqual(self._ids("is_active")[0], str(self.ada.id))
        self.assertEqual(self._ids("-is_active")[-1], str(self.ada.id))

    def test_the_dropdown_accepts_a_name_sort(self):
        """It lacked the annotation a name sort resolves to, so this raised instead."""
        response = self.client_alex.get(f"{URL}dropdown/", {"ordering": "name"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
