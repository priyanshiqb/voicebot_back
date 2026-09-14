"""`GET /onboard/billing-breakdown/` — the registrant's price, fetched again on demand.

Exists for a page that has been refreshed: the breakdown `verify-otp/` returned is gone
from memory, and the only way back was `resume-session-token/`, which reissues the token
and works out the next step as a side effect. This is the read-only version.

Two properties matter most, and most of these tests are one or the other:

* it returns **exactly** what `verify-otp/` returned — the customer must never see two
  different prices for the same choice;
* `sellable` **agrees with checkout** — a price the page shows and `create-order` then
  refuses is worse than no price.

Run with ``--settings=config.settings.test_postgres``.
"""

from decimal import Decimal

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from onboard.models import TempUser
from onboard.tests.billing_factories import (
    DEFAULT_RATES,
    Operation,
    make_minutes_plan,
    make_plan,
    make_tenant,
)

HIGH = Operation.CONVERSATION_HIGH_LATENCY


class BillingBreakdownTests(APITestCase):

    def setUp(self):
        self.plan = make_minutes_plan(
            min_minutes=501, max_minutes=2000, credit_basis=HIGH,
            discount_percentage=Decimal("0.00"), tax_percentage=Decimal("18.00"),
        )
        self.url = reverse("api-v1:billing-breakdown")
        self.register_url = reverse("api-v1:register-pending")
        self.verify_otp_url = reverse("api-v1:verify-otp")
        self.create_order_url = reverse("api-v1:create-order-just-in-time")

    # -- helpers ----------------------------------------------------------- #

    def _signup(self, plan, minutes=None, email="jane@example.com"):
        """Register and verify, leaving the client holding the temp token.

        Returns the temp user and the ``verify-otp`` response data, so a test can
        compare against the breakdown the customer was first shown.
        """
        payload = {
            "plan_id": str(plan.id),
            "first_name": "Jane", "last_name": "Doe",
            "business_name": "Doe Enterprises", "email": email,
            "password": "SecurePassword123!",
            "confirm_password": "SecurePassword123!",
        }
        if minutes is not None:
            payload["minutes"] = minutes
        registered = self.client.post(self.register_url, payload, format="json")
        self.assertEqual(registered.status_code, status.HTTP_201_CREATED, registered.data)

        temp_user = TempUser.objects.get(
            id=registered.data["data"]["temp_registration_id"]
        )
        verified = self.client.post(
            self.verify_otp_url,
            {"temp_registration_id": str(temp_user.id), "otp_code": temp_user.otp_code},
            format="json",
        )
        self.assertEqual(verified.status_code, status.HTTP_200_OK, verified.data)

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {verified.data['data']['temporary_token']}"
        )
        return temp_user, verified.data["data"]

    # -- what it returns --------------------------------------------------- #

    def test_returns_the_selected_minutes_and_their_breakdown(self):
        self._signup(self.plan, 1200)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertEqual(data["selected_minutes"], 1200)
        self.assertEqual(data["billing_breakdown"]["net_base_price"], "2160.00")
        self.assertEqual(data["billing_breakdown"]["total_price"], "2548.80")
        self.assertEqual(
            data["billing_breakdown"]["allocated_credits"], 1200 * DEFAULT_RATES[HIGH]
        )

    def test_the_breakdown_is_identical_to_what_verify_otp_returned(self):
        """The guarantee the endpoint exists for: a refresh must never show the
        customer a different price for the same choice."""
        _, at_verification = self._signup(self.plan, 1200)

        after_refresh = self.client.get(self.url).data["data"]

        self.assertEqual(
            after_refresh["billing_breakdown"], at_verification["billing_breakdown"]
        )

    def test_the_plan_block_carries_the_band(self):
        """So a refreshed page can re-render the minutes selector without a second
        request to the plans API."""
        self._signup(self.plan, 1200)

        plan = self.client.get(self.url).data["data"]["plan"]

        self.assertEqual(plan["id"], str(self.plan.id))
        self.assertEqual(plan["min_minutes"], 501)
        self.assertEqual(plan["max_minutes"], 2000)
        self.assertFalse(plan["is_fixed_quantity"])
        self.assertTrue(plan["is_minutes_based"])

    def test_it_follows_an_updated_plan_and_quantity(self):
        self._signup(self.plan, 1200)
        smaller = make_minutes_plan(
            min_minutes=None, max_minutes=500, credit_basis=HIGH,
            discount_percentage=Decimal("0.00"), tax_percentage=Decimal("18.00"),
        )
        self.client.patch(
            reverse("api-v1:update-plan"),
            {"plan_id": str(smaller.id), "minutes": 300},
            format="json",
        )

        data = self.client.get(self.url).data["data"]

        self.assertEqual(data["plan"]["id"], str(smaller.id))
        self.assertEqual(data["selected_minutes"], 300)

    # -- sellable agrees with checkout ------------------------------------- #

    def test_a_normal_selection_is_sellable(self):
        self._signup(self.plan, 1200)

        data = self.client.get(self.url).data["data"]

        self.assertTrue(data["sellable"])
        self.assertIsNone(data["reason"])

    def test_a_band_edited_after_selection_is_reported_unsellable(self):
        """The price is still returned, so the page can show what changed."""
        self._signup(self.plan, 1200)
        self.plan.max_minutes = 1000
        self.plan.save(update_fields=["max_minutes"])

        data = self.client.get(self.url).data["data"]

        self.assertFalse(data["sellable"])
        self.assertIn("1,200", data["reason"])
        self.assertEqual(data["billing_breakdown"]["selected_minutes"], 1200)

    def test_unsellable_here_means_checkout_refuses_too(self):
        """The two must never disagree — that is the only reason the flag exists."""
        self._signup(self.plan, 1200)
        self.plan.max_minutes = 1000
        self.plan.save(update_fields=["max_minutes"])

        breakdown = self.client.get(self.url).data["data"]
        checkout = self.client.post(
            self.create_order_url,
            {
                "phone_number": "919876543211",
                "billing_address": {
                    "line1": "123 Tech Square", "city": "Bengaluru",
                    "state": "Karnataka", "zip_code": "560001", "country": "IN",
                },
            },
            format="json",
        )

        self.assertFalse(breakdown["sellable"])
        self.assertEqual(checkout.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("MINUTES_NOT_SELLABLE", checkout.data["errors"])

    def test_a_plan_not_sold_by_the_minute_has_no_quantity(self):
        """Mirrors create-order, which does not quote such a plan either."""
        legacy = make_plan(credit_basis=None)
        self._signup(legacy)

        data = self.client.get(self.url).data["data"]

        self.assertIsNone(data["selected_minutes"])
        self.assertFalse(data["plan"]["is_minutes_based"])
        self.assertTrue(data["sellable"])

    # -- no side effects ---------------------------------------------------- #

    def test_it_does_not_reissue_the_token(self):
        """That is resume-session-token's job. A read that rotates credentials is not
        safe to call on every render."""
        self._signup(self.plan, 1200)

        data = self.client.get(self.url).data["data"]

        self.assertNotIn("temporary_token", data)

    def test_it_changes_nothing_on_the_registration(self):
        temp_user, _ = self._signup(self.plan, 1200)
        before = TempUser.objects.get(pk=temp_user.pk)

        for _ in range(3):
            self.client.get(self.url)

        after = TempUser.objects.get(pk=temp_user.pk)
        self.assertEqual(after.updated_at, before.updated_at)
        self.assertEqual(after.selected_minutes, 1200)

    def test_it_is_read_only(self):
        self._signup(self.plan, 1200)

        self.assertEqual(
            self.client.post(self.url, {}, format="json").status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    # -- who may call it ----------------------------------------------------- #

    def test_no_plan_selected_is_a_400(self):
        temp_user, _ = self._signup(self.plan, 1200)
        TempUser.objects.filter(pk=temp_user.pk).update(selected_plan=None)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("PLAN_SELECTION_REQUIRED", response.data["errors"])

    def test_no_token_is_refused(self):
        """403, the same as resume-session-token. TempUserAuthentication sends no
        WWW-Authenticate header, so DRF reports a missing token as 403 rather than
        401 -- and the frontend handles the two endpoints as one flow, so they must
        agree."""
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_tampered_token_is_refused(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer not-a-real-token")

        response = self.client.get(self.url)

        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_a_signed_in_tenant_user_is_refused(self):
        """A real account has a subscription, not a pending registration."""
        ctx = make_tenant()

        response = ctx.client.get(self.url)

        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )
