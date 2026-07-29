"""Contact CRUD, tenant isolation, lookup, tags, and export."""

from django.test import override_settings
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.models.contact import Contact
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models import CustomUser as User

BASE = "/api/v1/core/contacts/"


class ContactAPITestCase(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-api")
        self.other_tenant = Tenant.objects.create(name="Globex", subdomain="globex-api")

        self.user = User.objects.create_user(
            username="alex", email="alex@example.com", password="password"
        )
        self.other_user = User.objects.create_user(
            username="bob", email="bob@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)
        TenantUserMapping.objects.create(user=self.other_user, tenant=self.other_tenant)

        self.client_alex = APIClient()
        self.client_alex.force_authenticate(user=self.user)
        self.client_bob = APIClient()
        self.client_bob.force_authenticate(user=self.other_user)

        self.contact = Contact.objects.create(
            tenant=self.tenant,
            first_name="Ada",
            last_name="Lovelace",
            phone_number="+14155552671",
            email_address="ada@example.com",
            company="Analytical Engines",
            tags=["vip"],
        )


class ContactCrudTests(ContactAPITestCase):
    def test_create_normalizes_phone(self):
        response = self.client_alex.post(
            BASE,
            {"first_name": "Grace", "phone_number": "+1 (415) 555-2672"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["data"]["phone_e164"], "+14155552672")
        self.assertEqual(response.data["data"]["phone_number"], "+1 (415) 555-2672")

    def test_create_assigns_tenant_and_audit_fields(self):
        response = self.client_alex.post(
            BASE, {"phone_number": "+14155552673"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        created = Contact.objects.get(id=response.data["data"]["id"])
        self.assertEqual(created.tenant_id, self.tenant.id)
        self.assertEqual(created.created_by_id, self.user.id)

    def test_client_cannot_set_phone_e164(self):
        response = self.client_alex.post(
            BASE,
            {"phone_number": "+14155552674", "phone_e164": "+10000000000"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["data"]["phone_e164"], "+14155552674")

    def test_invalid_phone_is_a_field_error_not_a_crash(self):
        response = self.client_alex.post(
            BASE, {"phone_number": "notaphone"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("phone_number", response.data["errors"])

    def test_duplicate_phone_is_a_clean_400(self):
        """Must be a field error, not a 500 from the IntegrityError."""
        response = self.client_alex.post(
            BASE, {"phone_number": "+1 (415) 555-2671"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("phone_number", response.data["errors"])

    def test_update_contact(self):
        response = self.client_alex.patch(
            f"{BASE}{self.contact.id}/", {"company": "Difference Engines"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.company, "Difference Engines")
        self.assertEqual(self.contact.updated_by_id, self.user.id)

    def test_update_to_a_taken_phone_is_rejected(self):
        Contact.objects.create(tenant=self.tenant, phone_number="+14155552680")
        response = self.client_alex.patch(
            f"{BASE}{self.contact.id}/", {"phone_number": "+14155552680"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_keeping_own_phone_is_allowed(self):
        response = self.client_alex.patch(
            f"{BASE}{self.contact.id}/",
            {"phone_number": "+14155552671", "company": "Same Number"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_delete_is_soft_and_frees_the_number(self):
        response = self.client_alex.delete(f"{BASE}{self.contact.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.contact.refresh_from_db()
        self.assertTrue(self.contact.is_deleted)

        recreate = self.client_alex.post(
            BASE, {"phone_number": "+14155552671"}, format="json"
        )
        self.assertEqual(recreate.status_code, status.HTTP_201_CREATED)

    def test_tag_limit_enforced(self):
        with override_settings(CONTACT_MAX_TAGS_PER_CONTACT=2):
            response = self.client_alex.post(
                BASE,
                {"phone_number": "+14155552675", "tags": ["a", "b", "c"]},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("tags", response.data["errors"])

    def test_tags_are_trimmed_and_deduped(self):
        response = self.client_alex.post(
            BASE,
            {"phone_number": "+14155552676", "tags": ["  vip ", "vip", "", "lead"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["data"]["tags"], ["vip", "lead"])


class ContactIsolationTests(ContactAPITestCase):
    def test_list_is_tenant_scoped(self):
        response = self.client_alex.get(BASE)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)

        other = self.client_bob.get(BASE)
        self.assertEqual(len(other.data["data"]), 0)

    def test_retrieve_across_tenants_is_404(self):
        response = self.client_bob.get(f"{BASE}{self.contact.id}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_same_phone_allowed_in_a_different_tenant(self):
        response = self.client_bob.post(
            BASE, {"phone_number": "+14155552671"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_unauthenticated_is_rejected(self):
        response = APIClient().get(BASE)
        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )


class ContactLookupTests(ContactAPITestCase):
    def test_lookup_matches_regardless_of_formatting(self):
        for spelling in ["+14155552671", "+1 (415) 555-2671", "+1-415-555-2671"]:
            with self.subTest(spelling=spelling):
                response = self.client_alex.get(f"{BASE}lookup/", {"phone": spelling})
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(response.data["data"]["id"], str(self.contact.id))

    @override_settings(CONTACT_DEFAULT_PHONE_REGION="US")
    def test_lookup_with_national_format_and_region(self):
        response = self.client_alex.get(f"{BASE}lookup/", {"phone": "(415) 555-2671"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_lookup_unknown_number_is_404(self):
        response = self.client_alex.get(f"{BASE}lookup/", {"phone": "+14155559999"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_lookup_bad_number_is_400(self):
        response = self.client_alex.get(f"{BASE}lookup/", {"phone": "notaphone"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_lookup_does_not_leak_across_tenants(self):
        response = self.client_bob.get(f"{BASE}lookup/", {"phone": "+14155552671"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ContactTagsAndExportTests(ContactAPITestCase):
    def test_tags_facet(self):
        Contact.objects.create(
            tenant=self.tenant, phone_number="+14155552677", tags=["vip", "lead"]
        )
        response = self.client_alex.get(f"{BASE}tags/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        counts = {row["tag"]: row["count"] for row in response.data["data"]}
        self.assertEqual(counts, {"vip": 2, "lead": 1})

    def test_tags_facet_is_tenant_scoped(self):
        response = self.client_bob.get(f"{BASE}tags/")
        self.assertEqual(response.data["data"], [])

    def test_export_escapes_formula_injection(self):
        """A contact named =HYPERLINK(..) must not execute when opened in Excel."""
        Contact.objects.create(
            tenant=self.tenant,
            first_name='=HYPERLINK("http://evil","click")',
            phone_number="+14155552678",
        )
        response = self.client_alex.get(f"{BASE}export/", {"file_format": "csv"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        body = response.content.decode()
        self.assertIn("'=HYPERLINK", body)
        # The raw, unescaped form must not appear at the start of a field.
        self.assertNotIn(',=HYPERLINK', body)

    def test_export_is_tenant_scoped(self):
        response = self.client_bob.get(f"{BASE}export/", {"file_format": "csv"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("Lovelace", response.content.decode())

    def test_direct_import_is_disabled(self):
        """Bulk creation must go through the staged flow, not this endpoint."""
        response = self.client_alex.post(f"{BASE}import/", {}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
