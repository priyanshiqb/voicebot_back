"""
The company directory model.

`category` is a plain stored choice — set by the tenant, never inferred from the
job title. Nothing here tests classification because there is none to test.

What can still be wrong without looking wrong is the **partial unique
constraint**. Get either half of the condition wrong and it fails in a way nobody
hits until much later: soft-deleting an entry burns its address forever, or a
second contact with no email is refused.
"""

from django.db import IntegrityError, transaction
from django.test import TestCase

from core.models.directory_contact import DirectoryContact
from tenant.models.Tenant import Tenant


class SaveBehaviourTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-directory")

    def _make(self, **overrides):
        fields = {
            "tenant": self.tenant,
            "name": "Jane Doe",
            "designation": "VP of Engineering",
        }
        fields.update(overrides)
        return DirectoryContact.objects.create(**fields)

    def test_category_defaults_to_operations(self):
        self.assertEqual(
            self._make().category, DirectoryContact.Category.OPERATIONS
        )

    def test_the_category_is_stored_exactly_as_given(self):
        """Who counts as an executive is the tenant's call. Nothing reads the job
        title to decide, so renaming a title cannot move anyone between groups."""
        contact = self._make(category=DirectoryContact.Category.EXECUTIVE)
        self.assertEqual(contact.category, DirectoryContact.Category.EXECUTIVE)

        contact.designation = "Support Engineer"
        contact.save()

        contact.refresh_from_db()
        self.assertEqual(contact.category, DirectoryContact.Category.EXECUTIVE)

    def test_a_phone_number_is_normalized(self):
        contact = self._make(phone_number="+91 98765 43210")
        self.assertEqual(contact.phone_e164, "+919876543210")

    def test_an_unparseable_phone_is_kept_as_typed(self):
        """Unlike `Contact`, where the normalized number is the identity and must be
        valid. Here it is a dialling convenience, and a number a human can read is
        still worth storing."""
        contact = self._make(phone_number="x2214 (ask for Priya)")

        self.assertEqual(contact.phone_number, "x2214 (ask for Priya)")
        self.assertEqual(contact.phone_e164, "")

    def test_no_phone_leaves_the_normalized_column_blank(self):
        self.assertEqual(self._make().phone_e164, "")

    def test_housekeeping_saves_do_not_re_derive(self):
        """soft_delete passes update_fields; a row with an unclassifiable state must
        still be removable."""
        contact = self._make()
        contact.soft_delete()

        contact.refresh_from_db()
        self.assertTrue(contact.is_deleted)


class EmailUniquenessTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-unique")
        self.other = Tenant.objects.create(name="Globex", subdomain="globex-unique")

    def _make(self, tenant=None, email="", name="Someone"):
        return DirectoryContact.objects.create(
            tenant=tenant or self.tenant,
            name=name,
            designation="Support Engineer",
            email=email,
        )

    def test_the_same_address_twice_in_one_tenant_is_refused(self):
        self._make(email="jane@acme.com")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._make(email="jane@acme.com", name="Impostor")

    def test_two_tenants_may_hold_the_same_address(self):
        self._make(email="shared@example.com")
        self._make(tenant=self.other, email="shared@example.com")
        self.assertEqual(DirectoryContact.objects.count(), 2)

    def test_several_entries_may_have_no_email(self):
        """The half of the condition that makes `email` optional at all — without
        `~Q(email="")` the second blank collides with the first."""
        self._make(name="Reception")
        self._make(name="Warehouse Desk")
        self.assertEqual(DirectoryContact.objects.filter(email="").count(), 2)

    def test_soft_deleting_frees_the_address(self):
        """Without the `is_deleted` half, removing someone burns their address for
        good — and re-adding a colleague who left and came back would be
        impossible."""
        first = self._make(email="jane@acme.com")
        first.soft_delete()

        replacement = self._make(email="jane@acme.com", name="Jane Returns")
        self.assertNotEqual(replacement.pk, first.pk)
