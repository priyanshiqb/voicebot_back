"""Plan v2 — the minutes model on ``Plan``.

The whole legacy/v2 split lives in five methods on ``Plan``, and everything upstream
trusts them to give the right answer without knowing which generation of plan it is
holding. So these tests carry more weight than their size suggests: a wrong answer
here is a wrong price or a wrong credit grant everywhere.

Run with ``--settings=config.settings.test_postgres`` — the check constraints and the
JSON snapshot need a real database.
"""

from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from onboard.models import Plan, Subscription
from onboard.models.plan import MAX_SELECTABLE_MINUTES
from onboard.tests.billing_factories import (
    DEFAULT_MINUTE_PRICES,
    DEFAULT_RATES,
    Operation,
    add_rates,
    make_minutes_plan,
    make_plan,
    make_tenant,
)
from wallet.models import CreditRate

HIGH = Operation.CONVERSATION_HIGH_LATENCY
LOW = Operation.CONVERSATION_LOW_LATENCY
REEL = Operation.STATIC_REEL


class BandTests(TestCase):
    """``contains_minutes`` across every combination of open and closed bounds."""

    def test_closed_band_includes_both_bounds(self):
        plan = make_minutes_plan(min_minutes=501, max_minutes=2000)

        self.assertTrue(plan.contains_minutes(501))
        self.assertTrue(plan.contains_minutes(1200))
        self.assertTrue(plan.contains_minutes(2000))
        self.assertFalse(plan.contains_minutes(500))
        self.assertFalse(plan.contains_minutes(2001))

    def test_null_minimum_sells_anything_below_the_maximum(self):
        plan = make_minutes_plan(min_minutes=None, max_minutes=500)

        self.assertTrue(plan.contains_minutes(1))
        self.assertTrue(plan.contains_minutes(500))
        self.assertFalse(plan.contains_minutes(501))

    def test_null_maximum_sells_anything_above_the_minimum(self):
        plan = make_minutes_plan(min_minutes=2001, max_minutes=None)

        self.assertTrue(plan.contains_minutes(2001))
        self.assertTrue(plan.contains_minutes(999_999))
        self.assertFalse(plan.contains_minutes(2000))

    def test_an_open_band_is_still_bounded_by_the_system_ceiling(self):
        """"No maximum" means no maximum *this plan* imposes. A quantity past
        MAX_SELECTABLE_MINUTES is a typo, not an enterprise deal, and letting it
        through would carry it into a price no column can hold."""
        plan = make_minutes_plan(min_minutes=None, max_minutes=None)

        self.assertTrue(plan.contains_minutes(MAX_SELECTABLE_MINUTES))
        self.assertFalse(plan.contains_minutes(MAX_SELECTABLE_MINUTES + 1))

    def test_both_null_sells_any_quantity_up_to_the_ceiling(self):
        plan = make_minutes_plan(min_minutes=None, max_minutes=None)

        self.assertTrue(plan.contains_minutes(1))
        self.assertTrue(plan.contains_minutes(999_999))

    def test_zero_and_negative_are_never_in_a_band(self):
        """Not a purchase, whatever the bounds say."""
        plan = make_minutes_plan(min_minutes=None, max_minutes=None)

        self.assertFalse(plan.contains_minutes(0))
        self.assertFalse(plan.contains_minutes(-100))
        self.assertFalse(plan.contains_minutes(None))


class FixedQuantityTests(TestCase):
    """A pack is a band of width one, and needs no special handling."""

    def test_equal_bounds_make_a_pack(self):
        plan = make_minutes_plan(min_minutes=5000, max_minutes=5000)

        self.assertTrue(plan.is_fixed_quantity)
        self.assertTrue(plan.contains_minutes(5000))
        self.assertFalse(plan.contains_minutes(4999))
        self.assertFalse(plan.contains_minutes(5001))

    def test_a_pack_is_still_minutes_based(self):
        """The point of the encoding: selling one quantity says nothing about how it
        is priced, so every derivation still applies."""
        plan = make_minutes_plan(min_minutes=5000, max_minutes=5000)

        self.assertTrue(plan.is_minutes_based)
        self.assertEqual(plan.credits_for(5000), 5000 * DEFAULT_RATES[HIGH])
        self.assertEqual(
            plan.price_components(5000).net, Decimal("5000") * DEFAULT_MINUTE_PRICES[HIGH]
        )

    def test_a_range_is_not_a_pack(self):
        self.assertFalse(make_minutes_plan(min_minutes=1, max_minutes=500).is_fixed_quantity)

    def test_an_open_bound_is_not_a_pack(self):
        self.assertFalse(make_minutes_plan(min_minutes=5000).is_fixed_quantity)
        self.assertFalse(make_minutes_plan(max_minutes=5000).is_fixed_quantity)


class CreditsPerMinuteTests(TestCase):
    """Converting the basis row's rate into a per-minute figure."""

    def test_per_minute_basis_is_used_as_is(self):
        plan = make_minutes_plan(credit_basis=HIGH)
        self.assertEqual(plan.credits_per_minute(), DEFAULT_RATES[HIGH])

    def test_per_second_basis_is_scaled_by_sixty(self):
        plan = make_plan(with_rates=False, credit_basis=HIGH)
        add_rates(plan, {HIGH: (2, Decimal("1.80"))}, unit=CreditRate.Unit.PER_SECOND)

        self.assertEqual(plan.credits_per_minute(), 120)

    def test_per_event_basis_has_no_per_minute_meaning(self):
        """A flat per-call price cannot convert a quantity of minutes."""
        plan = make_plan(with_rates=False, credit_basis=HIGH)
        add_rates(plan, {HIGH: (5, Decimal("1.80"))}, unit=CreditRate.Unit.PER_EVENT)

        self.assertIsNone(plan.credits_per_minute())
        self.assertFalse(plan.is_minutes_based)

    def test_missing_basis_row_has_no_rate(self):
        plan = make_plan(with_rates=False, credit_basis=HIGH)
        self.assertIsNone(plan.credits_per_minute())

    def test_basis_reads_the_open_row_not_a_superseded_one(self):
        plan = make_minutes_plan(credit_basis=HIGH)
        old = plan.basis_rate()
        old.effective_to = timezone.now()
        old.save(update_fields=["effective_to"])
        add_rates(plan, {HIGH: (99, Decimal("9.90"))})

        plan.refresh_from_db()
        self.assertEqual(plan.credits_per_minute(), 99)

    def test_basis_cache_follows_a_reassigned_basis(self):
        """Cached per instance, keyed on the basis — so switching tiers in memory
        must not hand back the previous tier's row."""
        plan = make_minutes_plan(credit_basis=HIGH)
        self.assertEqual(plan.credits_per_minute(), DEFAULT_RATES[HIGH])

        plan.credit_basis = LOW
        self.assertEqual(plan.credits_per_minute(), DEFAULT_RATES[LOW])


class IsMinutesBasedTests(TestCase):
    """Which generation of plan this is — the gate every other method reads."""

    def test_a_plan_with_basis_and_price_is_minutes_based(self):
        self.assertTrue(make_minutes_plan().is_minutes_based)

    def test_a_plan_without_a_basis_is_not(self):
        """The only remaining way to be unsellable. Such a plan cannot be activated
        either — the gate refuses it."""
        self.assertFalse(make_plan(credit_basis=None).is_minutes_based)

    def test_a_basis_without_a_rupee_price_is_not(self):
        """The rate card prices calls in credits for every plan. Only a rupee price
        says the plan is *sold* by the minute."""
        plan = make_plan(with_rates=False, credit_basis=HIGH)
        add_rates(plan, {HIGH: 8})

        self.assertIsNone(plan.basis_rate().price_per_minute_inr)
        self.assertFalse(plan.is_minutes_based)

    def test_an_unsaved_plan_is_not(self):
        """No primary key means no rate rows to read."""
        self.assertFalse(Plan(code="x", name="x", credit_basis=HIGH).is_minutes_based)


class CreditsForTests(TestCase):
    """What a cycle grants."""

    def test_minutes_plan_multiplies_the_basis_rate(self):
        plan = make_minutes_plan(credit_basis=HIGH)
        self.assertEqual(plan.credits_for(1200), 1200 * DEFAULT_RATES[HIGH])

    def test_the_basis_decides_the_grant(self):
        """Same quantity, different tier sold at, different number of credits."""
        reel = make_minutes_plan(credit_basis=REEL)
        low = make_minutes_plan(credit_basis=LOW)

        self.assertEqual(reel.credits_for(1000), 1000 * DEFAULT_RATES[REEL])
        self.assertEqual(low.credits_for(1000), 1000 * DEFAULT_RATES[LOW])

    def test_a_plan_without_a_basis_grants_nothing(self):
        """There is no fixed allocation to fall back to any more. Zero rather than a
        guess: such a plan cannot be activated, so this is only reachable by a
        historical row — and its subscription carries its own grant."""
        plan = make_plan(credit_basis=None)

        self.assertEqual(plan.credits_for(9999), 0)
        self.assertEqual(plan.credits_for(None), 0)

    def test_a_minutes_plan_with_no_quantity_grants_nothing(self):
        self.assertEqual(make_minutes_plan().credits_for(None), 0)


class PriceComponentsTests(TestCase):
    """What a quantity costs, and that the parts add up."""

    def test_minutes_plan_prices_at_the_basis_rate(self):
        plan = make_minutes_plan(
            credit_basis=HIGH,
            discount_percentage=Decimal("0.00"),
            tax_percentage=Decimal("18.00"),
        )
        price = plan.price_components(1200)

        self.assertEqual(price.gross, Decimal("2160.00"))
        self.assertEqual(price.discount, Decimal("0.00"))
        self.assertEqual(price.net, Decimal("2160.00"))
        self.assertEqual(price.tax, Decimal("388.80"))
        self.assertEqual(price.total, Decimal("2548.80"))

    def test_discount_applies_to_the_computed_gross(self):
        plan = make_minutes_plan(
            credit_basis=HIGH,
            discount_percentage=Decimal("10.00"),
            tax_percentage=Decimal("18.00"),
        )
        price = plan.price_components(1000)

        self.assertEqual(price.gross, Decimal("1800.00"))
        self.assertEqual(price.discount, Decimal("180.00"))
        self.assertEqual(price.net, Decimal("1620.00"))
        self.assertEqual(price.tax, Decimal("291.60"))
        self.assertEqual(price.total, Decimal("1911.60"))

    def test_components_always_reconcile(self):
        """These five numbers are frozen into the snapshot as separate strings and
        read back independently. A total that is not the sum of its parts is a
        dispute nobody can settle."""
        plan = make_minutes_plan(
            credit_basis=LOW,
            discount_percentage=Decimal("7.50"),
            tax_percentage=Decimal("18.00"),
            minute_prices={LOW: Decimal("2.7350")},
        )
        for minutes in (1, 7, 333, 1201, 99_999):
            price = plan.price_components(minutes)
            with self.subTest(minutes=minutes):
                self.assertEqual(price.net, price.gross - price.discount)
                self.assertEqual(price.total, price.net + price.tax)
                self.assertEqual(price.total.as_tuple().exponent, -2)

    def test_sub_paise_rate_is_quantized_once(self):
        """₹1.8055 x 333 = ₹601.2315, which is not a payable amount."""
        plan = make_minutes_plan(
            credit_basis=HIGH,
            discount_percentage=Decimal("0.00"),
            tax_percentage=Decimal("0.00"),
            minute_prices={HIGH: Decimal("1.8055")},
        )
        self.assertEqual(plan.price_components(333).total, Decimal("601.23"))

    def test_a_plan_without_a_basis_cannot_be_priced(self):
        """Zero rather than a price. There is no `base_price` to fall back to, and
        quoting anything would put something on sale that has no price."""
        plan = make_plan(credit_basis=None, tax_percentage=Decimal("18.00"))
        price = plan.price_components(5000)

        self.assertEqual(price.gross, Decimal("0.00"))
        self.assertEqual(price.total, Decimal("0.00"))

    def test_the_catalogue_properties_report_the_entry_price(self):
        """`total_price` and friends are what the admin and the dropdown label render.
        With the price now a function of quantity they answer for the plan's smallest
        sellable one — the "from X" a pricing page shows."""
        plan = make_minutes_plan(
            min_minutes=501, max_minutes=2000, credit_basis=HIGH,
            discount_percentage=Decimal("10.00"), tax_percentage=Decimal("18.00"),
        )
        entry = plan.price_components(501)

        self.assertEqual(plan.discount_amount, entry.discount)
        self.assertEqual(plan.net_base_price, entry.net)
        self.assertEqual(plan.tax_amount, entry.tax)
        self.assertEqual(plan.total_price, entry.total)


class BandConstraintTests(TestCase):
    """``ck_plan_minutes_band_valid`` — the only new single-row fact."""

    def test_inverted_band_is_refused(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_plan(min_minutes=2000, max_minutes=500)

    def test_equal_bounds_are_allowed(self):
        """A pack has to be expressible."""
        plan = make_plan(min_minutes=5000, max_minutes=5000)
        self.assertTrue(plan.is_fixed_quantity)

    def test_open_bounds_are_allowed(self):
        make_plan(min_minutes=None, max_minutes=500)
        make_plan(min_minutes=500, max_minutes=None)
        make_plan(min_minutes=None, max_minutes=None)


class SnapshotTests(TestCase):
    """The frozen contract carries the quantity and both per-minute figures."""

    def _subscription(self, plan, minutes=None, credits=None):
        tenant = make_tenant().tenant
        return Subscription.objects.create(
            tenant=tenant, plan=plan, selected_minutes=minutes,
            allocated_credits=credits,
        )

    def test_minutes_subscription_freezes_the_derived_price(self):
        plan = make_minutes_plan(
            credit_basis=HIGH,
            discount_percentage=Decimal("0.00"),
            tax_percentage=Decimal("18.00"),
        )
        subscription = self._subscription(plan, minutes=1200, credits=9600)
        snapshot = subscription.plan_snapshot

        self.assertEqual(snapshot["selected_minutes"], 1200)
        self.assertEqual(snapshot["credit_basis"], HIGH)
        self.assertEqual(snapshot["price_per_minute_inr"], "1.8000")
        self.assertEqual(snapshot["credits_per_minute"], DEFAULT_RATES[HIGH])
        self.assertEqual(snapshot["net_base_price"], "2160.00")
        self.assertEqual(snapshot["total_price"], "2548.80")
        self.assertEqual(snapshot["allocated_credits"], 9600)

    def test_a_subscription_on_an_unsellable_plan_freezes_zeros(self):
        """Only reachable for a historical row whose plan has since lost its basis.
        It records that plainly rather than inventing a price."""
        plan = make_plan(credit_basis=None)
        snapshot = self._subscription(plan).plan_snapshot

        self.assertEqual(snapshot["base_price"], "0.00")
        self.assertEqual(snapshot["total_price"], "0.00")
        self.assertIsNone(snapshot["selected_minutes"])
        self.assertIsNone(snapshot["credit_basis"])
        self.assertIsNone(snapshot["price_per_minute_inr"])

    def test_a_superseded_rate_does_not_move_a_frozen_cycle(self):
        """The point of freezing both per-minute figures rather than looking them up."""
        plan = make_minutes_plan(credit_basis=HIGH)
        subscription = self._subscription(plan, minutes=1000, credits=8000)
        frozen = dict(subscription.plan_snapshot)

        old = plan.basis_rate()
        old.effective_to = timezone.now()
        old.save(update_fields=["effective_to"])
        add_rates(plan, {HIGH: (99, Decimal("99.00"))})

        subscription.refresh_from_db()
        self.assertEqual(subscription.plan_snapshot["price_per_minute_inr"],
                         frozen["price_per_minute_inr"])
        self.assertEqual(subscription.plan_snapshot["allocated_credits"], 8000)


class RulesTests(TestCase):
    """``rules`` resolves the grant without ever reaching for the plan."""

    def _subscription(self, plan, **kwargs):
        tenant = make_tenant().tenant
        return Subscription.objects.create(tenant=tenant, plan=plan, **kwargs)

    def test_rules_expose_the_quantity_and_the_grant(self):
        plan = make_minutes_plan(credit_basis=HIGH)
        rules = self._subscription(
            plan, selected_minutes=1200, allocated_credits=9600
        ).rules

        self.assertEqual(rules["selected_minutes"], 1200)
        self.assertEqual(rules["allocated_credits"], 9600)
        self.assertEqual(rules["credits_per_minute"], DEFAULT_RATES[HIGH])
        self.assertEqual(rules["credit_basis"], HIGH)

    def test_grant_comes_from_the_column_when_the_snapshot_predates_it(self):
        """The case that makes dropping ``Plan.allocated_credits`` safe."""
        plan = make_minutes_plan(credit_basis=HIGH)
        subscription = self._subscription(
            plan, selected_minutes=1000, allocated_credits=8000
        )
        subscription.plan_snapshot = {"code": plan.code}
        subscription.save(update_fields=["plan_snapshot"])

        self.assertEqual(subscription.rules["allocated_credits"], 8000)

    def test_grant_is_zero_rather_than_guessed_when_nothing_recorded_it(self):
        """The column is non-null now, so "nothing recorded" reads as zero — and zero
        is still an answer from the subscription rather than one borrowed from a plan
        that no longer has it."""
        plan = make_minutes_plan(credit_basis=HIGH)
        subscription = self._subscription(plan)
        subscription.plan_snapshot = {"code": plan.code}
        subscription.save(update_fields=["plan_snapshot"])

        self.assertEqual(subscription.rules["allocated_credits"], 0)

    def test_other_rules_still_fall_back_to_the_plan(self):
        plan = make_plan(allow_use_after_expiry=True)
        subscription = self._subscription(plan)
        subscription.plan_snapshot = {"code": plan.code}
        subscription.save(update_fields=["plan_snapshot"])

        self.assertTrue(subscription.rules["allow_use_after_expiry"])
