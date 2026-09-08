from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from onboard.models.plan import Plan
from user.models import CustomUser
from decimal import Decimal
from onboard.tests.billing_factories import make_plan


class SubscriptionTests(APITestCase):
    """
    Unit and integration tests for the Tenant Subscription model and automatic snapshotting behavior.
    """

    def setUp(self):
        from onboard.models.plan import Plan
        from tenant.models import Tenant

        # Create a free trial/tier plan
        self.free_plan = Plan.objects.create(
            # Live plan: `status` drives `is_active`, and it defaults to DRAFT.
            status=Plan.Status.ACTIVE,
            code="free_plan",
            name="Free Plan",
            description="Perfect for testing",
            discount_percentage=Decimal("0.00"),
            tax_percentage=Decimal("0.00"),
            currency=Plan.CurrencyChoices.INR,
            billing_cycle=Plan.BillingCycleChoices.MONTHLY,
            plan_type=Plan.PlanTypeChoices.FREE,
            sort_order=1,
            is_active=True
        )

        # Through the factory: a plan without a rate card has no price to freeze.
        # `base_price` is translated into a band and a per-minute price, so the
        # figures this test asserts are unchanged.
        self.paid_plan = make_plan(
            "premium_plan",
            name="Premium Plan",
            base_price=Decimal("150.00"),
            discount_percentage=Decimal("10.00"),
            tax_percentage=Decimal("18.00"),
            sort_order=2,
        )

        self.tenant = Tenant.objects.create(
            name="Acme Corp",
            subdomain="acme-test-subdomain"
        )

    def test_subscription_creation_and_plan_snapshotting(self):
        """
        Verify that creating a Subscription automatically captures and freezes
        the selected Plan details inside plan_snapshot to guard against future plan price updates.
        """
        from onboard.models import Subscription

        subscription = Subscription.objects.create(
            tenant=self.tenant,
            plan=self.paid_plan,
            status=Subscription.SubscriptionStatusChoices.ACTIVE
        )

        # Check that plan_snapshot is generated automatically
        self.assertIsNotNone(subscription.plan_snapshot)
        self.assertEqual(subscription.plan_snapshot["code"], "premium_plan")
        self.assertEqual(subscription.plan_snapshot["name"], self.paid_plan.name)
        self.assertEqual(subscription.plan_snapshot["base_price"], "150.00")
        self.assertEqual(subscription.plan_snapshot["total_price"], "159.30")  # 150 - 15 (discount) + 24.30 (tax) = 159.30

        # Change the original plan base price in the database
        self.paid_plan.base_price = Decimal("200.00")
        self.paid_plan.save()

        # The frozen snapshot on the subscription MUST NOT change!
        subscription.refresh_from_db()
        self.assertEqual(subscription.plan_snapshot["base_price"], "150.00")

    def test_free_plan_jit_checkout_bypass_onboarding(self):
        """
        Verify that JIT checkout with a 0 INR plan skips Razorpay order generation,
        automatically completes onboarding, provisions a Tenant and CustomUser,
        registers an active Subscription with a snapshot, and returns tokens.
        """
        from onboard.models import TempUser, Subscription
        from tenant.models import Tenant
        from django.core import signing

        temp_user = TempUser.objects.create(
            email="free.user@example.com",
            first_name="Free",
            last_name="Trialer",
            business_name="Free Business",
            selected_plan=self.free_plan,
            password_hash="some_secure_hash",
            is_email_verified=True
        )

        # Generate a temporary auth token
        temp_token = signing.dumps({"temp_user_id": str(temp_user.id)})

        checkout_url = "/api/v1/onboard/create-order-just-in-time/"
        payload = {
            "phone_number": "9876543210",
            "billing_address": {
                "line1": "123 Free Street",
                "city": "Freeport",
                "state": "Goa",
                "zip_code": "403001",
                "country": "India"
            }
        }

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {temp_token}")
        response = self.client.post(checkout_url, data=payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])
        self.assertTrue(response.data["data"]["is_free"])
        self.assertIsNotNone(response.data["data"]["access"])

        # Verify CustomUser was created
        from user.models import CustomUser
        user = CustomUser.objects.get(email="free.user@example.com")
        self.assertEqual(user.phone_number, "9876543210")

        # Verify Tenant was created
        tenant = Tenant.objects.get(name="Free Business")
        self.assertEqual(tenant.subdomain, "free-business")

        # Verify Subscription was created for the Tenant
        subscription = Subscription.objects.get(tenant=tenant)
        self.assertEqual(subscription.plan, self.free_plan)
        self.assertEqual(subscription.plan_snapshot["total_price"], "0.00")
        self.assertEqual(subscription.status, Subscription.SubscriptionStatusChoices.ACTIVE)

        # Verify TempUser was soft-deleted
        temp_user.refresh_from_db()
        self.assertTrue(temp_user.is_onboarded)
        self.assertTrue(temp_user.is_deleted)
