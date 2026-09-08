"""Plan v2 — the gate that stops a broken minutes ladder reaching customers.

A plan is only correct relative to its neighbours, so most of these checks are about
the ladder rather than the plan. They matter more than the field-level ones: a missing
band is not a limit of zero, it is an ambiguous or unanswerable question at the moment
somebody tries to buy.

Run with ``--settings=config.settings.test_postgres``.
"""

from decimal import Decimal

from django.test import TestCase

from onboard.models import Plan
from onboard.tests.billing_factories import (
    Operation,
    add_rates,
    make_minutes_plan,
    make_plan,
)
from ops.services.plan import config, lifecycle, readiness
from wallet.models import CreditRate

HIGH = Operation.CONVERSATION_HIGH_LATENCY
LOW = Operation.CONVERSATION_LOW_LATENCY
REEL = Operation.STATIC_REEL


def codes(problems):
    return [p["code"] for p in problems]


class MinutesBlockingTests(TestCase):
    """Activation is refused while any of these hold."""

    def test_a_complete_minutes_plan_can_activate(self):
        plan = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
            status=Plan.Status.DRAFT,
        )
        self.assertEqual(readiness.blocking_problems(plan), [])

    def test_a_plan_without_a_basis_is_refused(self):
        """Every plan is sold by the minute now: without a basis there is nothing to
        price it or grant against."""
        plan = make_plan(credit_basis=None, status=Plan.Status.DRAFT)
        self.assertIn("CREDIT_BASIS_UNSET", codes(readiness.blocking_problems(plan)))

    def test_a_band_without_a_basis_is_refused(self):
        plan = make_plan(
            credit_basis=None, min_minutes=1, max_minutes=500,
            status=Plan.Status.DRAFT,
        )
        self.assertIn("CREDIT_BASIS_UNSET", codes(readiness.blocking_problems(plan)))

    def test_a_basis_with_no_open_rate_is_refused(self):
        plan = make_plan(
            with_rates=False, credit_basis=HIGH, min_minutes=1, max_minutes=500,
            status=Plan.Status.DRAFT,
        )
        self.assertIn(
            "CREDIT_BASIS_RATE_MISSING", codes(readiness.blocking_problems(plan))
        )

    def test_a_basis_without_a_rupee_price_is_refused(self):
        """Credits alone do not make a plan sellable — nothing can price the sale."""
        plan = make_plan(
            with_rates=False, credit_basis=HIGH, min_minutes=1, max_minutes=500,
            status=Plan.Status.DRAFT,
        )
        add_rates(plan, {HIGH: 8})

        self.assertIn(
            "CREDIT_BASIS_NO_INR_PRICE", codes(readiness.blocking_problems(plan))
        )

    def test_a_per_event_basis_is_refused(self):
        plan = make_plan(
            with_rates=False, credit_basis=HIGH, min_minutes=1, max_minutes=500,
            status=Plan.Status.DRAFT,
        )
        add_rates(plan, {HIGH: (5, Decimal("1.80"))}, unit=CreditRate.Unit.PER_EVENT)

        self.assertIn(
            "CREDIT_BASIS_PER_EVENT", codes(readiness.blocking_problems(plan))
        )

    def test_a_free_plan_open_at_the_top_is_refused(self):
        """It would grant unbounded credits for nothing."""
        plan = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
            minute_prices={HIGH: Decimal("0.00")}, status=Plan.Status.DRAFT,
        )
        self.assertIn(
            "FREE_MINUTES_PLAN_UNCAPPED", codes(readiness.blocking_problems(plan))
        )

    def test_a_free_plan_with_a_ceiling_is_fine(self):
        plan = make_minutes_plan(
            min_minutes=None, max_minutes=100, credit_basis=HIGH,
            minute_prices={HIGH: Decimal("0.00")}, status=Plan.Status.DRAFT,
        )
        self.assertNotIn(
            "FREE_MINUTES_PLAN_UNCAPPED", codes(readiness.blocking_problems(plan))
        )


class LadderBlockingTests(TestCase):
    """A band is only correct relative to the plans around it."""

    def test_an_overlapping_band_cannot_activate(self):
        make_minutes_plan(min_minutes=None, max_minutes=2000, credit_basis=HIGH)
        candidate = make_minutes_plan(
            min_minutes=1000, max_minutes=3000, credit_basis=HIGH,
            status=Plan.Status.DRAFT,
        )

        self.assertIn("BAND_OVERLAPS", codes(readiness.blocking_problems(candidate)))
        with self.assertRaises(lifecycle.PlanNotReady):
            lifecycle.activate(candidate)

    def test_a_band_leaving_a_gap_cannot_activate(self):
        make_minutes_plan(min_minutes=None, max_minutes=500, credit_basis=HIGH)
        candidate = make_minutes_plan(
            min_minutes=1000, max_minutes=None, credit_basis=HIGH,
            status=Plan.Status.DRAFT,
        )

        self.assertIn("BAND_GAP", codes(readiness.blocking_problems(candidate)))

    def test_a_contiguous_band_activates(self):
        make_minutes_plan(min_minutes=None, max_minutes=500, credit_basis=HIGH)
        candidate = make_minutes_plan(
            min_minutes=501, max_minutes=None, credit_basis=HIGH,
            status=Plan.Status.DRAFT,
        )

        lifecycle.activate(candidate)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, Plan.Status.ACTIVE)

    def test_a_pack_beside_a_gap_activates(self):
        """Gaps between packs are what packs are for."""
        make_minutes_plan(min_minutes=1000, max_minutes=1000, credit_basis=HIGH)
        candidate = make_minutes_plan(
            min_minutes=5000, max_minutes=5000, credit_basis=HIGH,
            status=Plan.Status.DRAFT,
        )

        self.assertEqual(readiness.blocking_problems(candidate), [])

    def test_a_plan_does_not_overlap_itself_on_re_activation(self):
        """A retired plan coming back must be compared against the others, not
        against its own row in the ladder."""
        plan = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
        )
        plan.status = Plan.Status.RETIRED
        plan.save(update_fields=["status"])

        self.assertEqual(readiness.blocking_problems(plan), [])
        lifecycle.activate(plan)


class MinutesWarningTests(TestCase):

    def test_an_inconsistent_rupees_per_credit_is_warned(self):
        plan = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
            minute_prices={REEL: Decimal("0.10")},
        )
        found = [w["code"] for w in readiness.warnings(plan)]

        self.assertIn("RUPEES_PER_CREDIT_INCONSISTENT", found)

    def test_a_consistent_plan_is_not_warned(self):
        plan = make_minutes_plan(min_minutes=None, max_minutes=None, credit_basis=HIGH)
        found = [w["code"] for w in readiness.warnings(plan)]

        self.assertNotIn("RUPEES_PER_CREDIT_INCONSISTENT", found)

    def test_an_unpriced_non_basis_tier_is_warned(self):
        plan = make_plan(credit_basis=HIGH, with_rates=False)
        add_rates(plan, {HIGH: (8, Decimal("1.80")), LOW: 12, REEL: 4})

        found = [w["code"] for w in readiness.warnings(plan)]

        self.assertIn("RATE_MISSING_INR_PRICE", found)


class ReadinessReportTests(TestCase):
    """The console shows the ladder a plan is joining, not the plan alone."""

    def test_the_report_carries_the_whole_ladder(self):
        make_minutes_plan(min_minutes=None, max_minutes=500, credit_basis=HIGH,
                          minute_prices={HIGH: Decimal("2.75")})
        plan = make_minutes_plan(min_minutes=501, max_minutes=None, credit_basis=HIGH,
                                 minute_prices={HIGH: Decimal("1.80")})

        report = readiness.report(plan)

        self.assertEqual(len(report["ladder"]["bands"]), 2)
        self.assertTrue(any(b["is_this_plan"] for b in report["ladder"]["bands"]))
        self.assertEqual(len(report["ladder"]["boundaries"]), 1)

    def test_boundary_prices_are_reported_not_judged(self):
        """Every volume ladder is cheaper above a boundary; that is the discount, not
        a fault, so it is information rather than a warning."""
        make_minutes_plan(min_minutes=None, max_minutes=500, credit_basis=HIGH,
                          minute_prices={HIGH: Decimal("2.75")},
                          discount_percentage=Decimal("0.00"))
        plan = make_minutes_plan(min_minutes=501, max_minutes=None, credit_basis=HIGH,
                                 minute_prices={HIGH: Decimal("1.80")},
                                 discount_percentage=Decimal("0.00"))

        report = readiness.report(plan)

        self.assertTrue(report["ladder"]["boundaries"][0]["cheaper_above"])
        self.assertNotIn(
            "BAND_PRICE_CLIFF", [w["code"] for w in report["ladder"]["warnings"]]
        )


class RateWriteGuardTests(TestCase):
    """Activation cannot watch a rate edit that happens afterwards."""

    def setUp(self):
        self.plan = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
        )

    def _card(self, **basis_overrides):
        rows = []
        for operation in (REEL, HIGH, LOW):
            row = {
                "operation": operation,
                "credits_per_unit": 8,
                "price_per_minute_inr": Decimal("1.80"),
                "unit": CreditRate.Unit.PER_MINUTE,
                "rounding": CreditRate.Rounding.CEIL,
                "minimum_units": 1,
                "notes": "",
            }
            if operation == HIGH:
                row.update(basis_overrides)
            rows.append(row)
        return rows

    def test_blanking_a_live_plans_basis_price_is_refused(self):
        with self.assertRaises(config.RateWouldStrandBasis):
            config.replace_rates(
                self.plan, self._card(price_per_minute_inr=None), supersede=True
            )

    def test_making_a_live_plans_basis_per_event_is_refused(self):
        with self.assertRaises(config.RateWouldStrandBasis):
            config.replace_rates(
                self.plan,
                self._card(unit=CreditRate.Unit.PER_EVENT),
                supersede=True,
            )

    def test_an_ordinary_reprice_is_allowed(self):
        config.replace_rates(
            self.plan, self._card(price_per_minute_inr=Decimal("2.40")), supersede=True
        )

        self.plan.refresh_from_db()
        self.assertEqual(
            self.plan.basis_rate().price_per_minute_inr, Decimal("2.4000")
        )

    def test_a_draft_may_be_left_incomplete(self):
        """A draft on the way to being finished is not a broken live plan, and
        activation will refuse it anyway."""
        draft = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
            status=Plan.Status.DRAFT,
        )

        config.replace_rates(
            draft, self._card(price_per_minute_inr=None), supersede=False
        )

        self.assertIn(
            "CREDIT_BASIS_NO_INR_PRICE", codes(readiness.blocking_problems(draft))
        )
