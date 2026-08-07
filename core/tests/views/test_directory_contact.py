"""
Company directory API.

The parts worth guarding are the ones a passing smoke test would miss: tenant
isolation on every verb, that `?category=` filters on the stored column rather
than the job title, and that a duplicate email comes back as a field error
instead of a 500 from the database constraint.
"""

from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.models.directory_contact import DirectoryContact
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models import CustomUser as User

URL = "/api/v1/core/directory-contacts/"


class DirectoryContactTestCase(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-dir")
        self.other_tenant = Tenant.objects.create(name="Globex", subdomain="globex-dir")

        self.user = User.objects.create_user(
            username="dir-alex", email="dir-alex@example.com", password="password"
        )
        self.other_user = User.objects.create_user(
            username="dir-bob", email="dir-bob@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)
        TenantUserMapping.objects.create(user=self.other_user, tenant=self.other_tenant)

        self.client_alex = APIClient()
        self.client_alex.force_authenticate(user=self.user)
        self.client_bob = APIClient()
        self.client_bob.force_authenticate(user=self.other_user)

    def make(self, tenant=None, **overrides):
        fields = {
            "tenant": tenant or self.tenant,
            "name": "Jane Doe",
            "designation": "VP of Engineering",
            "category": DirectoryContact.Category.EXECUTIVE,
        }
        fields.update(overrides)
        return DirectoryContact.objects.create(**fields)

    def payload(self, **overrides):
        body = {
            "name": "Jane Doe",
            "designation": "VP of Engineering",
            "category": "EXECUTIVE",
            "department": "Engineering",
            "email": "jane@acme.com",
            "phone_number": "+91 98765 43210",
        }
        body.update(overrides)
        return body


class CrudTests(DirectoryContactTestCase):
    def test_create_returns_the_row_and_derives_the_normalized_phone(self):
        response = self.client_alex.post(URL, self.payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        data = response.data["data"]
        self.assertEqual(data["name"], "Jane Doe")
        self.assertEqual(data["category"], "EXECUTIVE")
        self.assertEqual(data["category_label"], "Executive")
        self.assertEqual(data["phone_e164"], "+919876543210")
        self.assertEqual(data["tenant"]["id"], str(self.tenant.id))

    def test_the_normalized_phone_cannot_be_set_by_a_client(self):
        """Accepting it would let the normalized column drift from the raw number
        every other surface displays."""
        response = self.client_alex.post(
            URL, self.payload(phone_e164="+10000000000"), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["data"]["phone_e164"], "+919876543210")

    def test_an_unparseable_phone_is_accepted_and_kept_as_typed(self):
        response = self.client_alex.post(
            URL, self.payload(phone_number="x2214 (ask for Priya)"), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["data"]["phone_number"], "x2214 (ask for Priya)")
        self.assertEqual(response.data["data"]["phone_e164"], "")

    def test_category_defaults_to_operations_when_omitted(self):
        body = self.payload()
        body.pop("category")
        response = self.client_alex.post(URL, body, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["data"]["category"], "OPERATIONS")

    def test_update_does_not_reclassify_on_a_title_change(self):
        contact = self.make()
        response = self.client_alex.patch(
            f"{URL}{contact.id}/", {"designation": "Support Engineer"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["data"]["category"], "EXECUTIVE")

    def test_delete_is_a_soft_delete(self):
        contact = self.make()
        response = self.client_alex.delete(f"{URL}{contact.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(DirectoryContact.all_objects.get(pk=contact.pk).is_deleted)
        self.assertFalse(DirectoryContact.objects.filter(pk=contact.pk).exists())


class EmailTests(DirectoryContactTestCase):
    def test_a_duplicate_email_is_a_field_error_not_a_500(self):
        self.make(email="jane@acme.com")
        response = self.client_alex.post(
            URL, self.payload(email="jane@acme.com"), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.data["errors"])

    def test_case_differences_do_not_defeat_the_constraint(self):
        """The constraint compares the column, so without normalising on the way in
        `Jane@` and `jane@` are two rows to Postgres and one address to a human."""
        self.make(email="jane@acme.com")
        response = self.client_alex.post(
            URL, self.payload(email="JANE@acme.com"), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_another_tenant_may_hold_the_same_address(self):
        self.make(email="shared@example.com")
        response = self.client_bob.post(
            URL, self.payload(email="shared@example.com"), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_several_entries_may_omit_the_email(self):
        self.client_alex.post(URL, self.payload(email="", name="Reception"), format="json")
        response = self.client_alex.post(
            URL, self.payload(email="", name="Warehouse"), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_updating_a_contact_does_not_clash_with_itself(self):
        contact = self.make(email="jane@acme.com")
        response = self.client_alex.patch(
            f"{URL}{contact.id}/", {"department": "Platform"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class FilteringTests(DirectoryContactTestCase):
    def setUp(self):
        super().setUp()
        self.exec_contact = self.make(
            name="Jane Doe",
            designation="VP of Engineering",
            category=DirectoryContact.Category.EXECUTIVE,
            email="jane@acme.com",
        )
        self.ops_contact = self.make(
            name="Sam Rao",
            designation="Support Engineer",
            category=DirectoryContact.Category.OPERATIONS,
            email="sam@acme.com",
            phone_number="+919812345678",
        )
        # Same tenant, but deliberately titled to look senior while filed under
        # Operations — the case that proves the filter reads the column.
        self.mislabelled = self.make(
            name="Kim Head",
            designation="Head of Overhead Lines",
            category=DirectoryContact.Category.OPERATIONS,
            email="kim@acme.com",
        )

    def _names(self, response):
        return {row["name"] for row in response.data["data"]}

    def test_listing_returns_everyone_alphabetically(self):
        response = self.client_alex.get(URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [row["name"] for row in response.data["data"]],
            ["Jane Doe", "Kim Head", "Sam Rao"],
        )

    def test_category_filters_on_the_stored_column_not_the_job_title(self):
        """"Head of Overhead Lines" is filed under Operations and must stay there —
        this is exactly what keyword matching on `designation` would get wrong."""
        response = self.client_alex.get(f"{URL}?category=EXECUTIVE")
        self.assertEqual(self._names(response), {"Jane Doe"})

        response = self.client_alex.get(f"{URL}?category=OPERATIONS")
        self.assertEqual(self._names(response), {"Sam Rao", "Kim Head"})

    def test_search_covers_name_designation_email_and_phone(self):
        for term, expected in (
            ("Jane", {"Jane Doe"}),
            ("Support", {"Sam Rao"}),
            ("kim@acme", {"Kim Head"}),
            ("9812345678", {"Sam Rao"}),
        ):
            with self.subTest(term=term):
                response = self.client_alex.get(f"{URL}?search={term}")
                self.assertEqual(self._names(response), expected)

    def test_the_dropdown_returns_label_value_pairs(self):
        response = self.client_alex.get(f"{URL}dropdown/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = response.data["data"][0]
        self.assertEqual(set(row), {"label", "value"})
        self.assertIn("—", row["label"])


class IsolationTests(DirectoryContactTestCase):
    def test_the_list_shows_only_your_own_tenant(self):
        self.make(name="Ours")
        self.make(tenant=self.other_tenant, name="Theirs")

        self.assertEqual(
            {row["name"] for row in self.client_alex.get(URL).data["data"]}, {"Ours"}
        )
        self.assertEqual(
            {row["name"] for row in self.client_bob.get(URL).data["data"]}, {"Theirs"}
        )

    def test_another_tenants_row_is_404_not_403(self):
        theirs = self.make(tenant=self.other_tenant)
        self.assertEqual(
            self.client_alex.get(f"{URL}{theirs.id}/").status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_another_tenant_cannot_update_or_delete_it(self):
        theirs = self.make(tenant=self.other_tenant, name="Theirs")

        patched = self.client_alex.patch(
            f"{URL}{theirs.id}/", {"name": "Hijacked"}, format="json"
        )
        deleted = self.client_alex.delete(f"{URL}{theirs.id}/")

        self.assertEqual(patched.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(deleted.status_code, status.HTTP_404_NOT_FOUND)
        theirs.refresh_from_db()
        self.assertEqual(theirs.name, "Theirs")
        self.assertFalse(theirs.is_deleted)

    def test_creating_assigns_the_callers_tenant(self):
        """The tenant is never taken from the body — naming another tenant's id has
        to be inert, not a way to write into it."""
        response = self.client_alex.post(
            URL, {**self.payload(), "tenant": str(self.other_tenant.id)}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            DirectoryContact.objects.get(pk=response.data["data"]["id"]).tenant_id,
            self.tenant.id,
        )

    def test_bulk_delete_is_scoped(self):
        theirs = self.make(tenant=self.other_tenant)
        response = self.client_alex.post(
            f"{URL}bulk_delete/", {"ids": [str(theirs.id)]}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        theirs.refresh_from_db()
        self.assertFalse(theirs.is_deleted)

    def test_authentication_is_required(self):
        self.assertEqual(
            APIClient().get(URL).status_code, status.HTTP_401_UNAUTHORIZED
        )
