"""
Contact model: phone normalization and the partial unique constraint.

These are the load-bearing guarantees for the whole module — conflict detection during
import and inbound-caller lookup both assume that two spellings of one number collapse
to a single identity.
"""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from core.models.contact import Contact
from tenant.models.Tenant import Tenant
from utils.phone import PhoneNormalizationError, normalize_phone


class PhoneNormalizationTests(TestCase):
    def test_spellings_collapse_to_one_e164(self):
        """The premise of conflict detection: one number, one identity."""
        spellings = [
            "+14155552671",
            "+1 (415) 555-2671",
            "+1-415-555-2671",
            "  +1 415 555 2671  ",
        ]
        results = {normalize_phone(s)[0] for s in spellings}
        self.assertEqual(results, {"+14155552671"})

    @override_settings(CONTACT_DEFAULT_PHONE_REGION="US")
    def test_national_format_uses_region(self):
        self.assertEqual(normalize_phone("(415) 555-2671")[0], "+14155552671")
        self.assertEqual(normalize_phone("415-555-2671")[0], "+14155552671")

    @override_settings(CONTACT_DEFAULT_PHONE_REGION="US")
    def test_explicit_region_overrides_default(self):
        self.assertEqual(normalize_phone("9876543210", region="IN")[0], "+919876543210")

    @override_settings(CONTACT_DEFAULT_PHONE_REGION="")
    def test_national_format_without_region_is_an_error(self):
        """D11: a missing region is an error, never a guess."""
        with self.assertRaises(PhoneNormalizationError) as ctx:
            normalize_phone("415-555-2671")
        self.assertIn("international format", str(ctx.exception))

    @override_settings(CONTACT_DEFAULT_PHONE_REGION="")
    def test_international_format_needs_no_region(self):
        self.assertEqual(normalize_phone("+919876543210")[0], "+919876543210")

    def test_invalid_numbers_rejected(self):
        for bad in ["notaphone", "12345", "+1555", "", "   ", None]:
            with self.subTest(value=bad):
                with self.assertRaises(PhoneNormalizationError):
                    normalize_phone(bad)

    def test_reserved_range_is_invalid(self):
        """555-01xx style reserved numbers must not pass as valid."""
        with self.assertRaises(PhoneNormalizationError):
            normalize_phone("+15551234567")

    @override_settings(CONTACT_DEFAULT_PHONE_REGION="ZZ")
    def test_unrecognised_region_is_an_error(self):
        with self.assertRaises(PhoneNormalizationError) as ctx:
            normalize_phone("415-555-2671")
        self.assertIn("not a recognised region", str(ctx.exception))

    def test_region_code_is_returned(self):
        self.assertEqual(normalize_phone("+14155552671")[1], "US")
        self.assertEqual(normalize_phone("+919876543210")[1], "IN")


class ContactSaveTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-contact")

    def test_save_derives_e164(self):
        c = Contact.objects.create(
            tenant=self.tenant, phone_number="+1 (415) 555-2671", first_name="Ada"
        )
        c.refresh_from_db()
        self.assertEqual(c.phone_e164, "+14155552671")
        self.assertEqual(c.phone_region, "US")
        # The raw value is preserved for display.
        self.assertEqual(c.phone_number, "+1 (415) 555-2671")

    def test_client_supplied_e164_is_ignored(self):
        """phone_e164 is derived, never trusted from input."""
        c = Contact.objects.create(
            tenant=self.tenant,
            phone_number="+14155552671",
            phone_e164="+10000000000",
        )
        c.refresh_from_db()
        self.assertEqual(c.phone_e164, "+14155552671")

    def test_invalid_number_raises_validation_error(self):
        with self.assertRaises(ValidationError):
            Contact.objects.create(tenant=self.tenant, phone_number="notaphone")

    def test_housekeeping_saves_survive_a_stale_number(self):
        """
        A row whose number is no longer parseable must still be deletable. Normalizing
        on every save would trap it, since soft_delete() only writes its own fields.
        """
        c = Contact.objects.create(tenant=self.tenant, phone_number="+14155552671")

        # Corrupt the raw value behind the model's back, as a bad legacy row would look.
        Contact.all_objects.filter(pk=c.pk).update(phone_number="garbage")
        c.refresh_from_db()

        c.soft_delete()  # must not raise
        c.refresh_from_db()
        self.assertTrue(c.is_deleted)

        c.deactivate()  # must not raise
        c.refresh_from_db()
        self.assertFalse(c.is_active)


class ContactUniquenessTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-uniq")
        self.other_tenant = Tenant.objects.create(name="Globex", subdomain="globex-uniq")

    def test_duplicate_active_phone_blocked(self):
        Contact.objects.create(tenant=self.tenant, phone_number="+14155552671")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Contact.objects.create(tenant=self.tenant, phone_number="+14155552671")

    def test_differently_formatted_duplicate_blocked(self):
        """The constraint bites on the normalized column, so formatting cannot evade it."""
        Contact.objects.create(tenant=self.tenant, phone_number="+14155552671")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Contact.objects.create(
                    tenant=self.tenant, phone_number="+1 (415) 555-2671"
                )

    def test_same_phone_allowed_across_tenants(self):
        Contact.objects.create(tenant=self.tenant, phone_number="+14155552671")
        other = Contact.objects.create(
            tenant=self.other_tenant, phone_number="+14155552671"
        )
        self.assertIsNotNone(other.pk)

    def test_soft_delete_frees_the_number(self):
        """A deleted contact must not permanently burn its phone number."""
        first = Contact.objects.create(tenant=self.tenant, phone_number="+14155552671")
        first.soft_delete()

        second = Contact.objects.create(tenant=self.tenant, phone_number="+14155552671")
        self.assertIsNotNone(second.pk)
        self.assertNotEqual(first.pk, second.pk)

    def test_restore_into_a_collision_is_refused_cleanly(self):
        """Must raise ValidationError, not a raw IntegrityError out of the admin."""
        first = Contact.objects.create(tenant=self.tenant, phone_number="+14155552671")
        first.soft_delete()
        Contact.objects.create(tenant=self.tenant, phone_number="+14155552671")

        with self.assertRaises(ValidationError):
            first.restore()

        first.refresh_from_db()
        self.assertTrue(first.is_deleted)

    def test_restore_succeeds_when_number_is_free(self):
        c = Contact.objects.create(tenant=self.tenant, phone_number="+14155552671")
        c.soft_delete()

        c.restore()
        c.refresh_from_db()
        self.assertFalse(c.is_deleted)
        self.assertIsNone(c.deleted_at)
