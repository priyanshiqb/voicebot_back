"""Plan v2 — nothing in the minutes path may raise on bad input.

A quantity is the field most likely to arrive wrong: it comes from query strings,
request bodies, JSON snapshots and command prompts, and a client can send a string, a
float, a null or a number with thirty digits. None of those is a server error.

Two rules, and every test here is one of them:

* the **pure helpers never raise** — unusable input is ``None``, the same state a
  legacy subscription is in, which every caller already handles;
* the **API edges refuse with a 4xx** — a readable field error, never a 500 and never
  a silently corrected number.

Run with ``--settings=config.settings.test_postgres``.
"""

from decimal import Decimal

from django.urls import reverse
from rest_framework.test import APITestCase

from onboard.models import Subscription, TempUser
from onboard.models.plan import MAX_SELECTABLE_MINUTES, coerce_minutes
from onboard.services import plan_selection, pricing
from onboard.tests.billing_factories import (
    BILLING,
    Operation,
    age_subscription,
    make_minutes_plan,
    make_plan,
    make_tenant,
    subscribe,
)

HIGH = Operation.CONVERSATION_HIGH_LATENCY
FOR_MINUTES = "/api/v1/onboard/plans/for-minutes/"

#: Everything a client, a JSON blob or a careless caller can realistically supply.
HOSTILE = [
    None, "", "abc", "12abc", "  ", [], {}, (), 0, -1, -10 ** 9,
    1.5, -1.5, float("nan"), float("inf"), float("-inf"),
    True, False, 10 ** 12, 10 ** 30, Decimal("NaN"), object(),
]


class CoerceMinutesTests(APITestCase):

    def test_nothing_makes_it_raise(self):
        for value in HOSTILE:
            with self.subTest(value=repr(value)):
                coerce_minutes(value)

    def test_usable_values_survive(self):
        self.assertEqual(coerce_minutes(1200), 1200)
        self.assertEqual(coerce_minutes("1200"), 1200)
        self.assertEqual(coerce_minutes(1200.0), 1200)
        self.assertEqual(coerce_minutes(Decimal("1200")), 1200)

    def test_everything_unusable_becomes_none(self):
        for value in HOSTILE:
            with self.subTest(value=repr(value)):
                self.assertIsNone(coerce_minutes(value))

    def test_true_is_not_one_minute(self):
        """``bool`` is an ``int`` subclass, so a JSON ``true`` would otherwise quietly
        become a one-minute purchase."""
        self.assertIsNone(coerce_minutes(True))


class PlanMethodTests(APITestCase):
    """The five methods every caller reaches for."""

    def setUp(self):
        self.plan = make_minutes_plan(
            min_minutes=501, max_minutes=2000, credit_basis=HIGH,
        )
        self.legacy = make_plan()

    def test_no_plan_method_raises_on_hostile_input(self):
        for plan in (self.plan, self.legacy):
            for value in HOSTILE:
                with self.subTest(plan=plan.code, value=repr(value)):
                    plan.contains_minutes(value)
                    plan.resolve_minutes(value)
                    plan.credits_for(value)
                    plan.price_components(value)

    def test_hostile_input_never_lands_inside_the_band(self):
        for value in HOSTILE:
            with self.subTest(value=repr(value)):
                self.assertFalse(self.plan.contains_minutes(value))

    def test_hostile_input_resolves_to_a_sellable_quantity(self):
        for value in HOSTILE:
            with self.subTest(value=repr(value)):
                resolved = self.plan.resolve_minutes(value)
                self.assertTrue(self.plan.contains_minutes(resolved))

    def test_hostile_input_never_produces_a_negative_price(self):
        for value in HOSTILE:
            with self.subTest(value=repr(value)):
                price = self.plan.price_components(value)
                self.assertGreaterEqual(price.total, Decimal("0.00"))
                self.assertEqual(price.total, price.net + price.tax)

    def test_hostile_input_never_produces_negative_credits(self):
        for value in HOSTILE:
            with self.subTest(value=repr(value)):
                self.assertGreaterEqual(self.plan.credits_for(value), 0)


class SelectionTests(APITestCase):

    def setUp(self):
        make_minutes_plan(min_minutes=None, max_minutes=None, credit_basis=HIGH)

    def test_for_minutes_never_raises(self):
        for value in HOSTILE:
            with self.subTest(value=repr(value)):
                plan_selection.for_minutes(value)

    def test_nearest_bands_never_raises(self):
        for value in HOSTILE:
            with self.subTest(value=repr(value)):
                plan_selection.nearest_bands(value)

    def test_hostile_input_matches_nothing(self):
        for value in HOSTILE:
            with self.subTest(value=repr(value)):
                self.assertEqual(plan_selection.for_minutes(value), (None, None))


class QuoteTests(APITestCase):
    """``quote_plan`` is the one that *should* raise — but only ``PricingError``."""

    def setUp(self):
        self.plan = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
        )

    def test_hostile_input_raises_only_pricing_error(self):
        for value in HOSTILE:
            with self.subTest(value=repr(value)):
                with self.assertRaises(pricing.PricingError):
                    pricing.quote_plan(self.plan, value)

    def test_a_quote_too_large_for_the_amount_column_is_refused(self):
        """``Transaction.amount`` is decimal(10,2). A quote above that would fail at
        insert time — a 500 in the middle of a checkout."""
        plan = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
            minute_prices={HIGH: Decimal("9999.9999")},
        )

        with self.assertRaises(pricing.PricingError):
            pricing.quote_plan(plan, MAX_SELECTABLE_MINUTES)

    def test_the_largest_realistic_quote_is_still_allowed(self):
        quote = pricing.quote_plan(self.plan, MAX_SELECTABLE_MINUTES)
        self.assertGreater(quote.total, Decimal("0.00"))


class PublicEndpointTests(APITestCase):
    """`for-minutes/` is unauthenticated, so it is the most exposed surface here."""

    def setUp(self):
        make_minutes_plan(min_minutes=None, max_minutes=None, credit_basis=HIGH)

    def test_no_query_string_produces_a_server_error(self):
        for value in ["", "abc", "1.5", "-1", "0", "NaN", "Infinity",
                      "1" * 400, "10000000000000", "[]", "null", "true"]:
            with self.subTest(value=value):
                response = self.client.get(FOR_MINUTES, {"minutes": value})
                self.assertLess(response.status_code, 500, value)
                self.assertIn(response.status_code, (400, 404), value)

    def test_a_hostile_billing_cycle_is_not_a_server_error(self):
        response = self.client.get(
            FOR_MINUTES, {"minutes": 1000, "billing_cycle": "'; DROP TABLE--"}
        )
        self.assertLess(response.status_code, 500)


class SignupEdgeTests(APITestCase):
    """The quantity arrives before a tenant exists, so it must be bounded here."""

    def setUp(self):
        self.plan = make_minutes_plan(
            min_minutes=501, max_minutes=2000, credit_basis=HIGH,
        )
        self.url = reverse("api-v1:register-pending")

    def _register(self, minutes, email):
        return self.client.post(
            self.url,
            {
                "plan_id": str(self.plan.id),
                "first_name": "Jane", "last_name": "Doe",
                "business_name": "Doe Enterprises", "email": email,
                "password": "SecurePassword123!",
                "confirm_password": "SecurePassword123!",
                "minutes": minutes,
            },
            format="json",
        )

    def test_hostile_quantities_are_refused_with_a_field_error(self):
        for index, value in enumerate(
            ["abc", -1, 0, 1.5, 10 ** 12, 10 ** 30, None, "", [], {}]
        ):
            with self.subTest(value=repr(value)):
                response = self._register(value, f"h{index}@example.com")
                self.assertEqual(response.status_code, 400, repr(value))

    def test_a_value_too_large_for_the_column_never_reaches_the_database(self):
        """Without a ceiling on the field this is a 500 from PostgreSQL."""
        response = self._register(MAX_SELECTABLE_MINUTES + 1, "big@example.com")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(TempUser.objects.filter(email="big@example.com").exists())


class ChangePlanEdgeTests(APITestCase):

    def setUp(self):
        self.ctx = make_tenant("hostile")
        self.client = self.ctx.client
        self.plan = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
        )
        self.subscription = subscribe(self.ctx.tenant, self.plan, minutes=1000)
        age_subscription(self.subscription, days_in=10)

    def test_hostile_quantities_never_produce_a_server_error(self):
        for value in ["abc", -1, 0, 1.5, 10 ** 12, 10 ** 30, [], {}, True]:
            with self.subTest(value=repr(value)):
                response = self.client.post(
                    f"{BILLING}/subscription/change-plan/",
                    {"plan_id": str(self.plan.pk), "minutes": value},
                    format="json",
                )
                self.assertLess(response.status_code, 500, repr(value))

    def test_a_hostile_quantity_changes_nothing(self):
        self.client.post(
            f"{BILLING}/subscription/change-plan/",
            {"plan_id": str(self.plan.pk), "minutes": 10 ** 12},
            format="json",
        )

        live = Subscription.objects.get(pk=self.subscription.pk)
        self.assertEqual(live.selected_minutes, 1000)
        self.assertEqual(live.status, "active")

    def test_a_hostile_eligible_plans_filter_is_ignored_not_fatal(self):
        for value in ["abc", "-1", "1e999", "[]"]:
            with self.subTest(value=value):
                response = self.client.get(
                    f"{BILLING}/subscription/eligible-plans/", {"minutes": value}
                )
                self.assertEqual(response.status_code, 200, value)
