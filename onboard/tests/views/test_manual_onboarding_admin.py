"""The Manual onboarding admin screen: who may use it, the TOTP step-up on submit, and
the audit trail it leaves."""

import json

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice

from onboard.constants.entitlement_resource import EntitlementResource
from onboard.constants.reset_period import ResetPeriod
from onboard.forms.manual_onboarding import entitlement_field_names
from onboard.models import ManualOnboarding
from onboard.tests.billing_factories import (
    make_minutes_plan,
    make_plan,
    make_tenant,
    subscribe,
)
from tenant.models import Tenant
from user.models import OperatorAuditLog
from user.models.CustomUser import CustomUser

ADD_URL = reverse("admin:onboard_manualonboarding_add")
LIST_URL = reverse("admin:onboard_manualonboarding_changelist")


def details_data(plan, minutes=1000, **overrides):
    data = {
        "plan": str(plan.pk),
        "minutes": str(minutes),
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


class ManualOnboardingAdminTests(TestCase):
    def setUp(self):
        self.plan = make_minutes_plan(min_minutes=1000, max_minutes=10000)
        self.operator, self.device = self.make_operator(is_superuser=True)

    # --- helpers -------------------------------------------------------------------

    def make_operator(self, *, is_superuser=False, email="ops@voicebot.example"):
        user = CustomUser.objects.create_user(
            email=email, password="x", is_staff=True, is_superuser=is_superuser,
        )
        device = TOTPDevice.objects.create(user=user, name="phone", confirmed=True)
        return user, device

    def sign_in(self, user, device):
        """A session that passed admin 2FA, as AdminSiteOTPRequired demands."""
        self.client.force_login(user)
        session = self.client.session
        session[DEVICE_ID_SESSION_KEY] = device.persistent_id
        session.save()

    def code(self, device):
        device.refresh_from_db()
        return f"{totp(device.bin_key, device.step, device.t0, device.digits, device.drift):0{device.digits}d}"

    def review(self, **overrides):
        response = self.client.post(ADD_URL, details_data(self.plan, _action="review", **overrides))
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context["review"], response.context["form"].errors)
        return response.context["review_digest"]

    def create(self, token, digest=None, **overrides):
        digest = digest or self.review(**overrides)
        return self.client.post(ADD_URL, details_data(
            self.plan, _action="create", review_digest=digest, otp_token=token, **overrides,
        ))

    # --- access --------------------------------------------------------------------

    def test_anonymous_is_sent_to_sign_in(self):
        response = self.client.get(ADD_URL)
        self.assertEqual(response.status_code, 302)

    def test_staff_without_the_permission_is_refused(self):
        staff, device = self.make_operator(email="support@voicebot.example")
        self.sign_in(staff, device)
        self.assertEqual(self.client.get(ADD_URL).status_code, 403)

    def test_staff_granted_the_permission_can_onboard(self):
        staff, device = self.make_operator(email="support@voicebot.example")
        staff.user_permissions.add(
            Permission.objects.get(codename="add_manualonboarding"),
            Permission.objects.get(codename="view_manualonboarding"),
        )
        self.sign_in(staff, device)

        self.assertEqual(self.client.get(ADD_URL).status_code, 200)
        response = self.create(self.code(device))
        self.assertTemplateUsed(response, "admin/onboard/manualonboarding/done.html")
        audit = OperatorAuditLog.objects.get(outcome=OperatorAuditLog.Outcome.SUCCESS)
        self.assertEqual(audit.actor, staff)

    # --- the steps -----------------------------------------------------------------

    def test_first_step_lists_only_plans_that_can_be_activated(self):
        unsellable = make_plan(credit_basis=None)
        self.sign_in(self.operator, self.device)

        response = self.client.get(ADD_URL)

        choices = set(response.context["plan_form"].fields["plan"].queryset)
        self.assertIn(self.plan, choices)
        self.assertNotIn(unsellable, choices)

    def test_second_step_is_prefilled_from_the_plan(self):
        self.sign_in(self.operator, self.device)
        response = self.client.get(ADD_URL, {"plan": str(self.plan.pk), "minutes": "1000"})
        form = response.context["form"]
        self.assertEqual(str(form.fields["amount"].initial), "2124.00")
        self.assertEqual(form.fields["credits"].initial, 8000)

    def test_review_shows_the_summary_and_writes_nothing(self):
        self.sign_in(self.operator, self.device)
        self.review()
        self.assertFalse(CustomUser.objects.filter(email="asha@acme.example").exists())
        self.assertFalse(OperatorAuditLog.objects.exists())

    # --- the TOTP step-up ------------------------------------------------------------

    def test_create_without_a_code_writes_nothing(self):
        self.sign_in(self.operator, self.device)
        response = self.create("")
        self.assertIn("otp_token", response.context["form"].errors)
        self.assertFalse(Tenant.objects.filter(name="Acme Industries").exists())
        # An empty box is a slip, not an attempt.
        self.assertFalse(OperatorAuditLog.objects.exists())

    def test_a_wrong_code_writes_nothing_and_is_audited(self):
        self.sign_in(self.operator, self.device)
        good = self.code(self.device)
        wrong = f"{(int(good) + 1) % 1_000_000:06d}"

        response = self.create(wrong)

        self.assertIn("otp_token", response.context["form"].errors)
        self.assertFalse(Tenant.objects.filter(name="Acme Industries").exists())
        failure = OperatorAuditLog.objects.get()
        self.assertEqual(failure.outcome, OperatorAuditLog.Outcome.FAILED)
        self.assertEqual(failure.actor, self.operator)
        self.assertEqual(failure.action, OperatorAuditLog.Action.TENANT_ONBOARD)
        self.assertIsNone(failure.tenant)

    def test_a_valid_code_onboards_and_records_who_did_it(self):
        self.sign_in(self.operator, self.device)

        response = self.create(self.code(self.device))

        self.assertTemplateUsed(response, "admin/onboard/manualonboarding/done.html")
        tenant = Tenant.objects.get(name="Acme Industries")
        subscription = response.context["subscription"]
        self.assertEqual(subscription.tenant, tenant)

        audit = OperatorAuditLog.objects.get()
        self.assertEqual(audit.outcome, OperatorAuditLog.Outcome.SUCCESS)
        self.assertEqual(audit.actor, self.operator)
        self.assertEqual(audit.action, OperatorAuditLog.Action.TENANT_ONBOARD)
        self.assertEqual(audit.tenant, tenant)
        self.assertEqual(audit.target, str(subscription.pk))
        self.assertEqual(audit.reason, "Signed enterprise contract, PO 4471")
        self.assertEqual(audit.payload["payment"]["reference"], "UTR123456789")
        self.assertEqual(audit.payload["verified_with"], "phone")

        # The generated password is on the result page, and nowhere in the audit row.
        password = response.context["generated_password"]
        self.assertTrue(password)
        self.assertContains(response, password)
        self.assertNotIn(password, json.dumps(audit.payload))
        self.assertTrue(CustomUser.objects.get(email="asha@acme.example").check_password(password))

    def test_a_code_cannot_be_used_twice(self):
        self.sign_in(self.operator, self.device)
        token = self.code(self.device)
        self.create(token)

        response = self.create(
            token, email="second@acme.example", payment_reference="UTR-SECOND",
            business_name="Second Co",
        )

        self.assertIn("otp_token", response.context["form"].errors)
        self.assertFalse(Tenant.objects.filter(name="Second Co").exists())

    def test_a_value_changed_after_review_is_not_created(self):
        self.sign_in(self.operator, self.device)
        digest = self.review()

        response = self.client.post(ADD_URL, details_data(
            self.plan, _action="create", review_digest=digest,
            otp_token=self.code(self.device), credits="999999",
        ))

        self.assertIn("changed since the review", response.context["notice"])
        self.assertFalse(Tenant.objects.filter(name="Acme Industries").exists())
        self.assertFalse(OperatorAuditLog.objects.exists())

    # --- the list --------------------------------------------------------------------

    def test_list_shows_manual_deals_only_with_their_operator(self):
        self.sign_in(self.operator, self.device)
        self.create(self.code(self.device))
        ordinary = make_tenant("ordinary")
        subscribe(ordinary, self.plan)

        response = self.client.get(LIST_URL)

        listed = {row.tenant.name for row in response.context["cl"].result_list}
        self.assertEqual(listed, {"Acme Industries"})
        self.assertContains(response, self.operator.email)

    def test_records_cannot_be_edited_or_deleted(self):
        self.sign_in(self.operator, self.device)
        self.create(self.code(self.device))
        record = ManualOnboarding.objects.get(tenant__name="Acme Industries")

        change_url = reverse("admin:onboard_manualonboarding_change", args=[record.pk])
        delete_url = reverse("admin:onboard_manualonboarding_delete", args=[record.pk])
        page = self.client.get(change_url)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="_save"')
        self.assertEqual(self.client.post(delete_url, {"post": "yes"}).status_code, 403)
