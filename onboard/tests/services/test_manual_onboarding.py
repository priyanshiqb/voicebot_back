"""onboard.services.manual_onboarding.provision: the sequence shared by the
``onboard_custom_deal`` command and the Manual onboarding admin screen."""

from decimal import Decimal
from unittest import mock

from django.test import TestCase

from onboard.constants.entitlement_resource import EntitlementResource
from onboard.constants.reset_period import ResetPeriod
from onboard.models import BillingAddress, ManualOnboarding, TempUser, Transaction
from onboard.services import manual_onboarding
from onboard.tests.billing_factories import make_minutes_plan
from tenant.models import Tenant, TenantUserMapping
from user.models.CustomUser import CustomUser


def deal_kwargs(plan, **overrides):
    kwargs = {
        "registrant": {
            "business_name": "Acme Industries",
            "first_name": "Asha",
            "last_name": "Rao",
            "email": "asha@acme.example",
            "phone_number": "+919812345678",
            "password": "correct horse battery staple",
            "referral_code": "",
            "timezone": None,
            "language": None,
        },
        "address": {
            "line1": "12 MG Road", "city": "Bengaluru", "state": "Karnataka",
            "zip_code": "560001", "country": "India", "gstin": "",
        },
        "plan": plan,
        "payment": {"amount": Decimal("150000.00"), "reference": "UTR998877"},
        "terms": {
            "credits": 120000,
            "max_rollover_credits": 5000,
            "low_balance_threshold": 1000,
            "allow_rollover_on_renewal": True,
            "allow_rollover_on_upgrade": True,
            "allow_rollover_on_downgrade": False,
            "allow_use_after_expiry": False,
            "renews_without_payment": False,
        },
        "entitlements": {
            resource.value: {"included": None, "reset_period": ResetPeriod.NONE}
            for resource in EntitlementResource
        },
        "minutes": 15000,
    }
    kwargs["entitlements"][EntitlementResource.AGENT.value] = {
        "included": 50, "reset_period": ResetPeriod.NONE,
    }
    kwargs.update(overrides)
    return kwargs


class ProvisionTests(TestCase):
    def setUp(self):
        self.plan = make_minutes_plan(min_minutes=1000, max_minutes=10000)

    def test_creates_an_ordinary_tenant_on_the_negotiated_terms(self):
        subscription = manual_onboarding.provision(**deal_kwargs(self.plan))

        user = CustomUser.objects.get(email="asha@acme.example")
        self.assertTrue(user.check_password("correct horse battery staple"))
        self.assertTrue(user.is_verified)

        tenant = subscription.tenant
        self.assertEqual(tenant.name, "Acme Industries")
        self.assertEqual(
            TenantUserMapping.objects.get(user=user, tenant=tenant).role,
            TenantUserMapping.Role.OWNER,
        )

        txn = Transaction.objects.get(subscription=subscription)
        self.assertTrue(txn.razorpay_order_id.startswith(manual_onboarding.MANUAL_ORDER_PREFIX))
        self.assertEqual(txn.razorpay_payment_id, "UTR998877")
        self.assertEqual(txn.status, Transaction.TransactionStatusChoices.SUCCESSFUL)
        self.assertEqual(txn.amount, Decimal("150000.00"))
        self.assertEqual((txn.user, txn.tenant, txn.temp_user), (user, tenant, None))

        snapshot = subscription.plan_snapshot
        self.assertEqual(snapshot["manual_payment_reference"], "UTR998877")
        self.assertEqual(snapshot["total_price"], "150000.00")
        self.assertEqual(snapshot["allocated_credits"], 120000)
        self.assertEqual(snapshot["entitlements"]["agent"]["included"], 50)

        self.assertTrue(BillingAddress.objects.filter(user=user, city="Bengaluru").exists())

        temp_user = TempUser.all_objects.get(email="asha@acme.example")
        self.assertTrue(temp_user.is_onboarded)
        self.assertTrue(temp_user.is_deleted)

        # The proxy the admin lists recognises it as a manual deal.
        self.assertTrue(
            ManualOnboarding.objects.filter(
                pk=subscription.pk, plan_snapshot__has_key="manual_payment_reference"
            ).exists()
        )

    def test_address_is_optional(self):
        subscription = manual_onboarding.provision(**deal_kwargs(self.plan, address=None))
        self.assertFalse(
            BillingAddress.objects.filter(user__email="asha@acme.example").exists()
        )
        self.assertIsNotNone(subscription.pk)

    def test_a_failure_writes_nothing(self):
        with mock.patch.object(
            manual_onboarding.subscription_service, "activate",
            side_effect=RuntimeError("activation refused"),
        ):
            with self.assertRaises(RuntimeError):
                manual_onboarding.provision(**deal_kwargs(self.plan))

        self.assertFalse(CustomUser.objects.filter(email="asha@acme.example").exists())
        self.assertFalse(Tenant.objects.filter(name="Acme Industries").exists())
        self.assertFalse(Transaction.objects.filter(razorpay_payment_id="UTR998877").exists())
        self.assertFalse(TempUser.all_objects.filter(email="asha@acme.example").exists())
