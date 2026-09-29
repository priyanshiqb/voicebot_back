"""Validation and defaults on the Manual onboarding admin forms."""

from decimal import Decimal

from django.test import TestCase

from onboard.constants.entitlement_resource import EntitlementResource
from onboard.constants.reset_period import ResetPeriod
from onboard.forms.manual_onboarding import (
    ManualOnboardingForm,
    PlanStepForm,
    entitlement_field_names,
)
from onboard.models import Transaction
from onboard.tests.billing_factories import make_minutes_plan, make_plan, make_transaction
from user.models.CustomUser import CustomUser


def form_data(**overrides):
    data = {
        "business_name": "Acme Industries",
        "first_name": "Asha",
        "last_name": "Rao",
        "email": "asha@acme.example",
        "phone_number": "+919812345678",
        "password": "",
        "referral_code": "",
        "timezone": "",
        "language": "",
        "record_address": "on",
        "line1": "12 MG Road",
        "city": "Bengaluru",
        "state": "Karnataka",
        "zip_code": "560001",
        "country": "India",
        "gstin": "",
        "amount": "2124.00",
        "payment_reference": "UTR123456789",
        "credits": "8000",
        "max_rollover_credits": "0",
        "low_balance_threshold": "50",
        "allow_rollover_on_renewal": "on",
        "reason": "Signed enterprise contract, PO 4471",
        "otp_token": "",
    }
    for resource in EntitlementResource:
        included, period = entitlement_field_names(resource)
        data[included] = "10" if resource == EntitlementResource.STORAGE_GB else ""
        data[period] = ResetPeriod.NONE
    data.update(overrides)
    return data


class PlanStepFormTests(TestCase):
    def test_offers_only_plans_that_can_be_activated(self):
        sellable = make_minutes_plan(min_minutes=1000, max_minutes=10000)
        unsellable = make_plan(credit_basis=None)

        choices = set(PlanStepForm().fields["plan"].queryset)

        self.assertIn(sellable, choices)
        self.assertNotIn(unsellable, choices)

    def test_blank_minutes_falls_back_to_the_plans_minimum(self):
        plan = make_minutes_plan(min_minutes=1000, max_minutes=10000)
        form = PlanStepForm({"plan": str(plan.pk), "minutes": ""})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["minutes"], 1000)

    def test_minutes_outside_the_band_are_accepted(self):
        plan = make_minutes_plan(min_minutes=1000, max_minutes=10000)
        form = PlanStepForm({"plan": str(plan.pk), "minutes": "50000"})
        self.assertTrue(form.is_valid(), form.errors)


class ManualOnboardingFormTests(TestCase):
    def setUp(self):
        self.plan = make_minutes_plan(min_minutes=1000, max_minutes=10000)

    def form(self, **overrides):
        return ManualOnboardingForm(form_data(**overrides), plan=self.plan, minutes=1000)

    def test_defaults_come_from_the_plan_and_minutes(self):
        form = ManualOnboardingForm(plan=self.plan, minutes=1000)
        # 1,000 minutes at Rs 1.80 plus 18% tax, and 8 credits a minute.
        self.assertEqual(form.fields["amount"].initial, Decimal("2124.00"))
        self.assertEqual(form.fields["credits"].initial, 8000)
        storage, _ = entitlement_field_names(EntitlementResource.STORAGE_GB)
        self.assertEqual(form.fields[storage].initial, 10)

    def test_a_valid_form_builds_what_the_service_expects(self):
        form = self.form(**{entitlement_field_names(EntitlementResource.AGENT)[0]: "25"})
        self.assertTrue(form.is_valid(), form.errors)

        entitlements = form.entitlements()
        self.assertEqual(entitlements["agent"], {"included": 25, "reset_period": ResetPeriod.NONE})
        self.assertIsNone(entitlements["user"]["included"])
        self.assertEqual(form.payment(), {"amount": Decimal("2124.00"), "reference": "UTR123456789"})
        self.assertEqual(form.address()["city"], "Bengaluru")

    def test_blank_password_is_generated_and_never_audited(self):
        form = self.form()
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(form.generated_password)
        password = form.cleaned_data["password"]
        self.assertGreaterEqual(len(password), 16)
        self.assertNotIn(password, str(form.audit_payload()))
        self.assertNotIn("password", form.audit_payload()["registrant"])

    def test_a_typed_password_must_pass_the_validators(self):
        form = self.form(password="123")
        self.assertFalse(form.is_valid())
        self.assertIn("password", form.errors)

    def test_email_already_in_use_is_refused(self):
        CustomUser.objects.create_user(email="Asha@Acme.example", password="x")
        form = self.form()
        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)

    def test_phone_is_normalised_to_e164(self):
        form = self.form(phone_number="+91 98123 45678")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["phone_number"], "+919812345678")

    def test_invalid_phone_is_refused(self):
        form = self.form(phone_number="+91 12")
        self.assertFalse(form.is_valid())
        self.assertIn("phone_number", form.errors)

    def test_a_payment_reference_already_recorded_is_refused(self):
        make_transaction(
            plan=self.plan,
            razorpay_payment_id="UTR123456789",
            status=Transaction.TransactionStatusChoices.SUCCESSFUL,
        )
        form = self.form()
        self.assertFalse(form.is_valid())
        self.assertIn("payment_reference", form.errors)

    def test_gstin_must_have_the_gstin_shape(self):
        self.assertIn("gstin", self.form(gstin="NOT-A-GSTIN").errors)
        good = self.form(gstin="29abcde1234f1z5")
        self.assertTrue(good.is_valid(), good.errors)
        self.assertEqual(good.cleaned_data["gstin"], "29ABCDE1234F1Z5")

    def test_address_fields_are_required_only_when_recording_one(self):
        missing = self.form(city="")
        self.assertFalse(missing.is_valid())
        self.assertIn("city", missing.errors)

        skipped = self.form(record_address="", line1="", city="", state="", zip_code="")
        self.assertTrue(skipped.is_valid(), skipped.errors)
        self.assertIsNone(skipped.address())

    def test_a_reason_is_required(self):
        self.assertIn("reason", self.form(reason="").errors)
        self.assertIn("reason", self.form(reason="ok").errors)

    def test_business_name_is_capped_for_the_subdomain(self):
        self.assertIn("business_name", self.form(business_name="x" * 91).errors)

    def test_negative_or_huge_numbers_are_refused(self):
        self.assertIn("credits", self.form(credits="-1").errors)
        self.assertIn("credits", self.form(credits=str(2**31)).errors)
        self.assertIn("amount", self.form(amount="-5").errors)

    def test_review_digest_changes_when_a_value_changes(self):
        first = self.form()
        second = self.form(credits="9000")
        self.assertTrue(first.is_valid() and second.is_valid())
        self.assertNotEqual(first.review_digest(), second.review_digest())
        again = self.form()
        self.assertTrue(again.is_valid())
        self.assertEqual(first.review_digest(), again.review_digest())
