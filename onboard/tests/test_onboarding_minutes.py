"""Plan v2 — carrying a quantity of minutes through signup.

The number is chosen before a tenant exists and has to survive OTP verification, a
plan switch, a resumed session and a payment before it can reach the subscription. It
also decides the amount charged, so every step that shows a price has to show the same
one.

Run with ``--settings=config.settings.test_postgres``.
"""

from decimal import Decimal

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from onboard.models import Subscription, TempUser, Transaction
from onboard.models.timezone import Timezone
from onboard.tests.billing_factories import (
    DEFAULT_RATES,
    Operation,
    make_minutes_plan,
    make_plan,
)
from wallet.models import CreditLedger, Wallet

HIGH = Operation.CONVERSATION_HIGH_LATENCY


class MinutesSignupTests(APITestCase):

    def setUp(self):
        self.plan = make_minutes_plan(
            min_minutes=501, max_minutes=2000, credit_basis=HIGH,
            discount_percentage=Decimal("0.00"), tax_percentage=Decimal("18.00"),
        )
        self.small_plan = make_minutes_plan(
            min_minutes=None, max_minutes=500, credit_basis=HIGH,
            discount_percentage=Decimal("0.00"), tax_percentage=Decimal("18.00"),
        )
        self.legacy_plan = make_plan(credit_basis=None)

        self.register_url = reverse("api-v1:register-pending")
        self.verify_otp_url = reverse("api-v1:verify-otp")
        self.update_plan_url = reverse("api-v1:update-plan")
        self.create_order_url = reverse("api-v1:create-order-just-in-time")
        self.verify_payment_url = reverse("api-v1:verify")
        self.resume_token_url = reverse("api-v1:resume-session-token")

    # -- helpers ----------------------------------------------------------- #

    def _register(self, plan, minutes=None, email="jane@example.com", **extra):
        payload = {
            "plan_id": str(plan.id),
            "first_name": "Jane",
            "last_name": "Doe",
            "business_name": "Doe Enterprises",
            "email": email,
            "password": "SecurePassword123!",
            "confirm_password": "SecurePassword123!",
            **extra,
        }
        if minutes is not None:
            payload["minutes"] = minutes
        return self.client.post(self.register_url, data=payload, format="json")

    def _verify_otp(self, temp_id):
        temp_user = TempUser.objects.get(id=temp_id)
        return self.client.post(
            self.verify_otp_url,
            data={"temp_registration_id": temp_id, "otp_code": temp_user.otp_code},
            format="json",
        )

    def _order(self):
        return self.client.post(
            self.create_order_url,
            data={
                "phone_number": "919876543211",
                "billing_address": {
                    "line1": "123 Tech Square", "city": "Bengaluru",
                    "state": "Karnataka", "zip_code": "560001", "country": "IN",
                },
            },
            format="json",
        )

    def _through_otp(self, plan, minutes):
        temp_id = self._register(plan, minutes).data["data"]["temp_registration_id"]
        token = self._verify_otp(temp_id).data["data"]["temporary_token"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return temp_id

    # -- registration ------------------------------------------------------ #

    def test_minutes_are_recorded_at_registration(self):
        response = self._register(self.plan, 1200)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        temp_user = TempUser.objects.get(id=response.data["data"]["temp_registration_id"])
        self.assertEqual(temp_user.selected_minutes, 1200)

    def test_a_minutes_plan_without_a_quantity_is_refused(self):
        response = self._register(self.plan, None)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("minutes", response.data["errors"])

    def test_a_quantity_outside_the_band_is_refused(self):
        """Refused rather than silently corrected — the client already asked
        for-minutes/ and got a quantity inside the band, so a mismatch means the two
        have got out of step."""
        response = self._register(self.plan, 5000)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_plan_not_sold_by_the_minute_refuses_a_quantity(self):
        response = self._register(self.legacy_plan, 1200)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_plan_not_sold_by_the_minute_registers_without_one(self):
        response = self._register(self.legacy_plan)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        temp_user = TempUser.objects.get(id=response.data["data"]["temp_registration_id"])
        self.assertIsNone(temp_user.selected_minutes)

    # -- the price shown at every step ------------------------------------- #

    def test_otp_verification_prices_the_chosen_quantity(self):
        temp_id = self._register(self.plan, 1200).data["data"]["temp_registration_id"]

        breakdown = self._verify_otp(temp_id).data["data"]["billing_breakdown"]

        self.assertEqual(breakdown["net_base_price"], "2160.00")
        self.assertEqual(breakdown["total_price"], "2548.80")
        self.assertEqual(breakdown["selected_minutes"], 1200)
        self.assertEqual(breakdown["allocated_credits"], 1200 * DEFAULT_RATES[HIGH])

    def test_resume_returns_the_quantity_and_its_price(self):
        self._through_otp(self.plan, 1200)

        data = self.client.post(self.resume_token_url, format="json").data["data"]

        self.assertEqual(data["selected_minutes"], 1200)
        self.assertEqual(data["billing_breakdown"]["total_price"], "2548.80")

    def test_a_plan_not_sold_by_the_minute_is_priced_at_nothing(self):
        temp_id = self._register(self.legacy_plan).data["data"]["temp_registration_id"]

        breakdown = self._verify_otp(temp_id).data["data"]["billing_breakdown"]

        self.assertEqual(breakdown["total_price"], "0.00")
        self.assertIsNone(breakdown["selected_minutes"])

    # -- switching plan ---------------------------------------------------- #

    def test_switching_plan_can_carry_a_new_quantity(self):
        self._through_otp(self.plan, 1200)

        response = self.client.patch(
            self.update_plan_url,
            data={"plan_id": str(self.small_plan.id), "minutes": 300},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["data"]["billing_breakdown"]["selected_minutes"], 300
        )

    def test_switching_to_a_plan_that_cannot_sell_the_quantity_is_refused(self):
        """1,200 minutes does not fit a plan selling up to 500. Caught here rather
        than at checkout, where the failure is far less explicable."""
        self._through_otp(self.plan, 1200)

        response = self.client.patch(
            self.update_plan_url,
            data={"plan_id": str(self.small_plan.id)},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["data"]["max_minutes"], 500)

    def test_switching_to_a_plan_without_a_basis_clears_the_quantity(self):
        self._through_otp(self.plan, 1200)

        response = self.client.patch(
            self.update_plan_url,
            data={"plan_id": str(self.legacy_plan.id)},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["data"]["billing_breakdown"]["selected_minutes"])

    # -- the order --------------------------------------------------------- #

    def test_the_order_is_raised_for_the_quoted_amount(self):
        self._through_otp(self.plan, 1200)

        response = self._order()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["amount"], 254880)

    def test_the_order_freezes_both_per_minute_figures(self):
        """So activation grants what was quoted, even if the rate card moves before
        the payment clears."""
        self._through_otp(self.plan, 1200)
        self._order()

        breakdown = Transaction.objects.get().amount_breakdown

        self.assertEqual(breakdown["selected_minutes"], "1200")
        self.assertEqual(breakdown["credits_per_minute"], str(DEFAULT_RATES[HIGH]))
        self.assertEqual(breakdown["total"], "2548.80")

    # -- payment ----------------------------------------------------------- #

    def test_payment_carries_the_quantity_onto_the_subscription(self):
        self._through_otp(self.plan, 1200)
        order_id = self._order().data["data"]["razorpay_order_id"]

        response = self.client.post(
            self.verify_payment_url,
            data={
                "razorpay_order_id": order_id,
                "razorpay_payment_id": "pay_test_1",
                "razorpay_signature": "sig",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        subscription = Subscription.objects.get()
        self.assertEqual(subscription.selected_minutes, 1200)
        self.assertEqual(subscription.allocated_credits, 1200 * DEFAULT_RATES[HIGH])

    def test_the_wallet_is_granted_what_the_subscription_records(self):
        """The invariant: the column and the ledger are the same number."""
        self._through_otp(self.plan, 1200)
        order_id = self._order().data["data"]["razorpay_order_id"]
        self.client.post(
            self.verify_payment_url,
            data={
                "razorpay_order_id": order_id,
                "razorpay_payment_id": "pay_test_2",
                "razorpay_signature": "sig",
            },
            format="json",
        )

        subscription = Subscription.objects.get()
        granted = CreditLedger.objects.get(
            entry_type=CreditLedger.EntryType.CYCLE_ALLOCATION
        )
        wallet = Wallet.objects.get(tenant=subscription.tenant)

        self.assertEqual(granted.amount, subscription.allocated_credits)
        self.assertEqual(wallet.balance, subscription.allocated_credits)


class FreeMinutesPlanTests(APITestCase):
    """The zero-cost path, which now runs through the shared promotion."""

    def setUp(self):
        self.plan = make_minutes_plan(
            min_minutes=None, max_minutes=100, credit_basis=HIGH,
            plan_type="free", base_price=Decimal("0.00"),
            tax_percentage=Decimal("0.00"),
            minute_prices={HIGH: Decimal("0.00")},
        )
        # TempUser.save() defaults its timezone to the seeded UTC row. The test
        # database has no seed, so without this the drift assertion below would be
        # measuring seed data rather than the promotion carrying it across.
        Timezone.objects.get_or_create(
            timezone="UTC", defaults={"offset": "+00:00", "region": "Etc"}
        )

        self.register_url = reverse("api-v1:register-pending")
        self.verify_otp_url = reverse("api-v1:verify-otp")
        self.create_order_url = reverse("api-v1:create-order-just-in-time")

    def test_a_free_minutes_plan_onboards_without_the_gateway(self):
        response = self.client.post(
            self.register_url,
            data={
                "plan_id": str(self.plan.id),
                "first_name": "Free", "last_name": "User",
                "business_name": "Free Co", "email": "free@example.com",
                "password": "SecurePassword123!",
                "confirm_password": "SecurePassword123!",
                "minutes": 100,
            },
            format="json",
        )
        temp_id = response.data["data"]["temp_registration_id"]
        temp_user = TempUser.objects.get(id=temp_id)
        token = self.client.post(
            self.verify_otp_url,
            data={"temp_registration_id": temp_id, "otp_code": temp_user.otp_code},
            format="json",
        ).data["data"]["temporary_token"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        response = self.client.post(
            self.create_order_url,
            data={
                "phone_number": "919876543212",
                "billing_address": {
                    "line1": "1 Free Lane", "city": "Bengaluru",
                    "state": "Karnataka", "zip_code": "560001", "country": "IN",
                },
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["data"]["is_free"])

        subscription = Subscription.objects.get()
        self.assertEqual(subscription.selected_minutes, 100)
        self.assertEqual(subscription.allocated_credits, 100 * DEFAULT_RATES[HIGH])

    def test_the_free_path_no_longer_drifts_from_the_shared_promotion(self):
        """It used to inline its own copy, which had already lost the registrant's
        timezone and language and never set transaction.tenant."""
        self.client.post(
            self.register_url,
            data={
                "plan_id": str(self.plan.id),
                "first_name": "Free", "last_name": "User",
                "business_name": "Free Co", "email": "free2@example.com",
                "password": "SecurePassword123!",
                "confirm_password": "SecurePassword123!",
                "minutes": 50,
            },
            format="json",
        )
        temp_user = TempUser.objects.get(email="free2@example.com")
        token = self.client.post(
            self.verify_otp_url,
            data={"temp_registration_id": str(temp_user.id),
                  "otp_code": temp_user.otp_code},
            format="json",
        ).data["data"]["temporary_token"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.client.post(
            self.create_order_url,
            data={
                "phone_number": "919876543213",
                "billing_address": {
                    "line1": "2 Free Lane", "city": "Bengaluru",
                    "state": "Karnataka", "zip_code": "560001", "country": "IN",
                },
            },
            format="json",
        )

        transaction = Transaction.objects.get()
        subscription = Subscription.objects.get()

        # The two things the hand-copied branch used to lose: the tenant on the
        # transaction (so a free signup's first payment appeared in their own billing
        # history) and the registrant's locale.
        self.assertEqual(transaction.tenant_id, subscription.tenant_id)
        self.assertIsNotNone(transaction.user.timezone_id)
