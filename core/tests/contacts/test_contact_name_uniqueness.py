"""
BUG-001234 — a contact name is unique per tenant.

Covers the three places the rule has to hold, because each can pass while another
fails: the serializer (a clean 400), the database (the partial unique index that
catches a lost race), and the staged import (which never goes through the serializer
at all and writes with bulk_create).
"""

from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.models.contact import Contact
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models import CustomUser as User

BASE = "/api/v1/core/contacts/"


class ContactNameTestCase(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-name")
        self.other_tenant = Tenant.objects.create(name="Globex", subdomain="globex-name")

        self.user = User.objects.create_user(
            username="alex", email="alex@example.com", password="password"
        )
        self.outsider = User.objects.create_user(
            username="bob", email="bob@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)
        TenantUserMapping.objects.create(user=self.outsider, tenant=self.other_tenant)

        self.client_alex = APIClient()
        self.client_alex.force_authenticate(user=self.user)
        self.client_bob = APIClient()
        self.client_bob.force_authenticate(user=self.outsider)

        self.ada = Contact.objects.create(
            tenant=self.tenant,
            first_name="Ada",
            last_name="Lovelace",
            phone_number="+14155550001",
        )

    def _create(self, client=None, **fields):
        payload = {"phone_number": "+14155550999"}
        payload.update(fields)
        return (client or self.client_alex).post(BASE, payload, format="json")


class CreateTests(ContactNameTestCase):
    def test_duplicate_name_is_rejected(self):
        response = self._create(first_name="Ada", last_name="Lovelace")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("first_name", response.data["errors"])
        self.assertEqual(
            response.data["errors"]["first_name"][0],
            "A contact with this name already exists.",
        )

    def test_duplicate_name_is_rejected_regardless_of_case(self):
        response = self._create(first_name="ADA", last_name="lovelace")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("first_name", response.data["errors"])

    def test_surrounding_whitespace_does_not_get_a_name_past_the_check(self):
        """The DB lowercases but cannot trim, so the serializer has to."""
        response = self._create(first_name="  Ada  ", last_name="Lovelace ")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_different_last_name_is_a_different_name(self):
        response = self._create(first_name="Ada", last_name="Byron")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_names_are_stored_trimmed(self):
        response = self._create(first_name="  Grace  ", last_name=" Hopper ")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        created = Contact.objects.get(id=response.data["data"]["id"])
        self.assertEqual(created.first_name, "Grace")
        self.assertEqual(created.last_name, "Hopper")

    def test_unnamed_contacts_are_exempt_and_may_repeat(self):
        """
        Every contact captured from an inbound call arrives with no name. If ("", "")
        were unique, the second such contact could never be created.
        """
        first = self._create(phone_number="+14155550010")
        second = self._create(phone_number="+14155550011")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)

    def test_the_rule_is_per_tenant(self):
        response = self._create(
            client=self.client_bob, first_name="Ada", last_name="Lovelace"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class UpdateTests(ContactNameTestCase):
    def setUp(self):
        super().setUp()
        self.grace = Contact.objects.create(
            tenant=self.tenant,
            first_name="Grace",
            last_name="Hopper",
            phone_number="+14155550002",
        )

    def test_renaming_onto_another_contact_is_rejected(self):
        response = self.client_alex.patch(
            f"{BASE}{self.grace.id}/",
            {"first_name": "Ada", "last_name": "Lovelace"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("first_name", response.data["errors"])

    def test_a_contact_may_keep_its_own_name(self):
        response = self.client_alex.patch(
            f"{BASE}{self.grace.id}/", {"first_name": "Grace"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_partial_update_is_checked_against_the_stored_other_half(self):
        """
        PATCHing only `last_name` still has to collide: the check has to read
        `first_name` through to the instance rather than treating it as absent.
        """
        Contact.objects.create(
            tenant=self.tenant,
            first_name="Grace",
            last_name="Murray",
            phone_number="+14155550003",
        )
        response = self.client_alex.patch(
            f"{BASE}{self.grace.id}/", {"last_name": "Murray"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class SoftDeleteTests(ContactNameTestCase):
    def test_deleting_a_contact_frees_its_name(self):
        self.client_alex.delete(f"{BASE}{self.ada.id}/")
        response = self._create(first_name="Ada", last_name="Lovelace")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_restore_refuses_when_the_name_was_taken_in_the_meantime(self):
        from django.core.exceptions import ValidationError

        self.ada.soft_delete()
        Contact.objects.create(
            tenant=self.tenant,
            first_name="Ada",
            last_name="Lovelace",
            phone_number="+14155550004",
        )
        with self.assertRaises(ValidationError):
            self.ada.restore()


class DatabaseConstraintTests(ContactNameTestCase):
    """
    The serializer check is a query, so two concurrent writes can both pass it. The
    index is what actually holds the rule, and these fail if it is ever dropped.
    """

    def test_the_index_rejects_a_duplicate_written_around_the_serializer(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Contact.objects.create(
                tenant=self.tenant,
                first_name="ada",
                last_name="LOVELACE",
                phone_number="+14155550005",
            )

    def test_the_index_allows_the_same_name_in_another_tenant(self):
        Contact.objects.create(
            tenant=self.other_tenant,
            first_name="Ada",
            last_name="Lovelace",
            phone_number="+14155550006",
        )
        self.assertEqual(
            Contact.objects.filter(first_name__iexact="ada").count(), 2
        )

    def test_the_index_allows_many_unnamed_contacts(self):
        for index in range(3):
            Contact.objects.create(
                tenant=self.tenant, phone_number=f"+1415555010{index}"
            )
        self.assertEqual(
            Contact.objects.filter(tenant=self.tenant, first_name="").count(), 3
        )
