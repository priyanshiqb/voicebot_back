"""Plan v2 — pricing a quantity of minutes.

``quote_plan`` is what every purchase path runs through, and what gets frozen onto
the order. ``quote_plan_change`` is the same sign test that already decides upgrade
from downgrade, now asked about a quantity as well as a plan.

Run with ``--settings=config.settings.test_postgres``.
"""

from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from onboard.services import pricing
from onboard.tests.billing_factories import (
    DEFAULT_MINUTE_PRICES,
    DEFAULT_RATES,
    Operation,
    make_minutes_plan,
    make_plan,
    subscribe,
    make_tenant,
)

HIGH = Operation.CONVERSATION_HIGH_LATENCY
LOW = Operation.CONVERSATION_LOW_LATENCY


class QuotePlanTests(TestCase):
    """Pricing a quantity on one plan."""

    def setUp(self):
        self.plan = make_minutes_plan(
            min_minutes=501,
            max_minutes=2000,
            credit_basis=HIGH,
            discount_percentage=Decimal("0.00"),
            tax_percentage=Decimal("18.00"),
        )

    def test_price_is_minutes_times_the_basis_rate(self):
        quote = pricing.quote_plan(self.plan, 1200)

        self.assertEqual(quote.net, Decimal("2160.00"))
        self.assertEqual(quote.tax, Decimal("388.80"))
        self.assertEqual(quote.total, Decimal("2548.80"))

    def test_quote_freezes_both_per_minute_figures(self):
        """The whole reason they are in the detail: an order raised today must not
        re-price or re-grant when the rate card moves tomorrow."""
        detail = pricing.quote_plan(self.plan, 1200).detail

        self.assertEqual(detail["selected_minutes"], 1200)
        self.assertEqual(detail["credit_basis"], HIGH)
        self.assertEqual(detail["price_per_minute_inr"], DEFAULT_MINUTE_PRICES[HIGH])
        self.assertEqual(detail["credits_per_minute"], DEFAULT_RATES[HIGH])
        self.assertEqual(detail["allocated_credits"], 1200 * DEFAULT_RATES[HIGH])

    def test_breakdown_survives_serialisation(self):
        """``as_breakdown()`` is what lands on Transaction.amount_breakdown, so every
        value has to be JSON-safe."""
        import json

        breakdown = pricing.quote_plan(self.plan, 1200).as_breakdown()
        json.dumps(breakdown)

        self.assertEqual(breakdown["selected_minutes"], "1200")
        self.assertEqual(breakdown["total"], "2548.80")

    def test_discount_reaches_the_computed_gross(self):
        plan = make_minutes_plan(
            credit_basis=HIGH,
            discount_percentage=Decimal("10.00"),
            tax_percentage=Decimal("18.00"),
        )
        quote = pricing.quote_plan(plan, 1000)

        self.assertEqual(quote.detail["gross"], Decimal("1800.00"))
        self.assertEqual(quote.detail["discount"], Decimal("180.00"))
        self.assertEqual(quote.net, Decimal("1620.00"))

    def test_quantity_outside_the_band_is_refused(self):
        with self.assertRaises(pricing.PricingError):
            pricing.quote_plan(self.plan, 500)
        with self.assertRaises(pricing.PricingError):
            pricing.quote_plan(self.plan, 2001)

    def test_the_error_says_what_the_plan_does_sell(self):
        with self.assertRaises(pricing.PricingError) as caught:
            pricing.quote_plan(self.plan, 5000)
        self.assertIn("501-2,000 minutes", str(caught.exception))

    def test_zero_and_negative_are_refused(self):
        for minutes in (0, -5, None):
            with self.subTest(minutes=minutes):
                with self.assertRaises(pricing.PricingError):
                    pricing.quote_plan(self.plan, minutes)

    def test_an_absurd_quantity_is_refused_rather_than_quoted(self):
        plan = make_minutes_plan(min_minutes=None, max_minutes=None)
        with self.assertRaises(pricing.PricingError):
            pricing.quote_plan(plan, pricing.MAX_SELECTABLE_MINUTES + 1)

    def test_a_plan_without_a_basis_cannot_be_quoted(self):
        with self.assertRaises(pricing.PricingError):
            pricing.quote_plan(make_plan(credit_basis=None), 1000)

    def test_a_pack_quotes_only_its_own_quantity(self):
        pack = make_minutes_plan(min_minutes=5000, max_minutes=5000, credit_basis=HIGH)

        self.assertEqual(
            pricing.quote_plan(pack, 5000).net, Decimal("5000") * DEFAULT_MINUTE_PRICES[HIGH]
        )
        with self.assertRaises(pricing.PricingError):
            pricing.quote_plan(pack, 4999)


class NetForTests(TestCase):
    """The join between the two generations, for callers that just want a price."""

    def test_minutes_plan(self):
        plan = make_minutes_plan(credit_basis=HIGH)
        self.assertEqual(pricing.net_for(plan, 1000), Decimal("1800.00"))

    def test_a_plan_without_a_basis_has_no_price(self):
        plan = make_plan(credit_basis=None, discount_percentage=Decimal("0.00"))
        self.assertEqual(pricing.net_for(plan, 9999), Decimal("0.00"))


class BonusDaysTests(TestCase):
    """Unused value buys days at the *destination's* daily rate."""

    def test_daily_rate_follows_the_quantity_bought(self):
        """A day of 2,000 minutes costs more than a day of 1,000, so the same money
        buys fewer days of it."""
        plan = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
            discount_percentage=Decimal("0.00"),
        )
        value = Decimal("1800.00")

        small = pricing.bonus_days_for(value, plan, minutes=1000)
        large = pricing.bonus_days_for(value, plan, minutes=2000)

        self.assertGreater(small, large)
        self.assertAlmostEqual(small / large, Decimal("2"), places=1)

    def test_a_plan_without_a_basis_buys_no_days(self):
        """Nothing to divide by: a plan with no price per day cannot be bought more
        of, so the remaining value has nowhere to go."""
        plan = make_plan(credit_basis=None, discount_percentage=Decimal("0.00"))
        self.assertEqual(
            pricing.bonus_days_for(Decimal("500.00"), plan, minutes=5000),
            Decimal("0.00"),
        )


class PlanChangeTests(TestCase):
    """The sign test, now asked about quantity as well as plan."""

    def setUp(self):
        self.tenant = make_tenant().tenant

    def _minutes_subscription(self, minutes=1000, **plan_kwargs):
        plan_kwargs.setdefault("min_minutes", None)
        plan_kwargs.setdefault("max_minutes", None)
        plan_kwargs.setdefault("credit_basis", HIGH)
        plan_kwargs.setdefault("discount_percentage", Decimal("0.00"))
        plan = make_minutes_plan(**plan_kwargs)
        subscription = subscribe(self.tenant, plan)
        subscription.selected_minutes = minutes
        subscription.allocated_credits = plan.credits_for(minutes)
        subscription.generate_plan_snapshot()
        subscription.save()
        return subscription, plan

    def test_more_minutes_on_the_same_plan_is_an_upgrade(self):
        """The genuinely new transaction shape, priced by the existing sign test."""
        subscription, plan = self._minutes_subscription(minutes=1000)

        quote = pricing.quote_plan_change(subscription, plan, minutes=2500)

        self.assertEqual(quote.detail["change_type"], pricing.PAID_CHANGE)
        self.assertEqual(quote.detail["selected_minutes"], 2500)
        self.assertGreater(quote.total, Decimal("0.00"))

    def test_fewer_minutes_on_the_same_plan_is_scheduled(self):
        subscription, plan = self._minutes_subscription(minutes=2000)

        quote = pricing.quote_plan_change(subscription, plan, minutes=600)

        self.assertEqual(quote.detail["change_type"], pricing.SCHEDULED_CHANGE)
        self.assertEqual(quote.total, Decimal("0.00"))

    def test_same_plan_same_minutes_is_refused(self):
        """The only request with nothing in it."""
        subscription, plan = self._minutes_subscription(minutes=1000)

        with self.assertRaises(pricing.PricingError):
            pricing.quote_plan_change(subscription, plan, minutes=1000)

    def test_minutes_default_to_what_the_tenant_already_has(self):
        """'Move me to that plan' means 'the same usage, over there'."""
        subscription, _ = self._minutes_subscription(minutes=1400)
        target = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=LOW,
            discount_percentage=Decimal("0.00"),
        )

        quote = pricing.quote_plan_change(subscription, target)

        self.assertEqual(quote.detail["selected_minutes"], 1400)

    def test_minutes_are_resolved_into_the_target_band(self):
        subscription, _ = self._minutes_subscription(minutes=300)
        target = make_minutes_plan(min_minutes=1000, max_minutes=2000, credit_basis=HIGH)

        quote = pricing.quote_plan_change(subscription, target)

        self.assertEqual(quote.detail["selected_minutes"], 1000)

    def test_moving_to_a_pack_takes_the_packs_quantity(self):
        subscription, _ = self._minutes_subscription(minutes=1400)
        pack = make_minutes_plan(min_minutes=5000, max_minutes=5000, credit_basis=HIGH)

        quote = pricing.quote_plan_change(subscription, pack)

        self.assertEqual(quote.detail["selected_minutes"], 5000)

    def test_quote_reports_the_credit_delta(self):
        subscription, plan = self._minutes_subscription(minutes=1000)

        quote = pricing.quote_plan_change(subscription, plan, minutes=2000)

        self.assertEqual(quote.detail["allocated_credits"], 2000 * DEFAULT_RATES[HIGH])
        self.assertEqual(quote.detail["credits_delta"], 1000 * DEFAULT_RATES[HIGH])

    def test_a_subscription_with_no_quantity_can_move_to_a_minutes_plan(self):
        """``old_net`` comes from their snapshot and ``new_net`` from the quote. The
        sign test does not care where either came from — which is what let a legacy
        subscriber upgrade during the migration."""
        legacy_plan = make_plan(credit_basis=None)
        subscription = subscribe(self.tenant, legacy_plan)
        target = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
            discount_percentage=Decimal("0.00"),
        )

        quote = pricing.quote_plan_change(subscription, target, minutes=2000)

        self.assertEqual(quote.detail["selected_minutes"], 2000)
        self.assertEqual(quote.detail["change_type"], pricing.PAID_CHANGE)

    def test_moving_to_a_plan_with_no_basis_is_priced_at_nothing(self):
        """Such a plan cannot be activated, so this is only reachable for a historical
        one. It costs nothing, which makes it a scheduled change rather than a sale."""
        subscription, _ = self._minutes_subscription(minutes=2000)
        stranded = make_plan(credit_basis=None)

        quote = pricing.quote_plan_change(subscription, stranded)

        self.assertIsNone(quote.detail["selected_minutes"])
        self.assertEqual(quote.total, Decimal("0.00"))


class CreditImpactTests(TestCase):
    """What the customer is told about their credits before they commit."""

    def test_incoming_allocation_follows_the_quantity(self):
        tenant = make_tenant().tenant
        plan = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
        )
        subscription = subscribe(tenant, plan)
        subscription.selected_minutes = 1000
        subscription.allocated_credits = plan.credits_for(1000)
        subscription.save()

        impact = pricing.credit_impact(
            subscription, plan, reason="upgrade", minutes=2500
        )

        self.assertEqual(impact["selected_minutes"], 2500)
        self.assertEqual(impact["new_plan_allocation"], 2500 * DEFAULT_RATES[HIGH])
        self.assertEqual(
            impact["balance_after_change"],
            impact["carried_forward"] + impact["purchased"] + impact["new_plan_allocation"],
        )
