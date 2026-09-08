"""The seeded minutes plans must be sellable, not merely loadable.

A seed file is configuration, and a plan is the one kind of configuration that fails
*silently* — a missing allowance reads as unlimited, a missing rate cannot price a
call, and an overlapping band gives the pricing page two answers. So this loads the
real files and puts them through the same activation gate a human would hit in the
console.

It is also the only test that would notice somebody hand-editing the JSON into
something that no longer sells.

Run with ``--settings=config.settings.test_postgres``.
"""

from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from onboard.models import Plan
from onboard.services import plan_selection, pricing
from ops.services.plan import readiness

SEED_FILES = ("plan_v2", "plan_entitlement_v2", "credit_rate_v2")


class SeededMinutesPlanTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        for name in SEED_FILES:
            call_command("load_data", name)
        cls.plans = list(Plan.objects.filter(code__endswith="_minutes"))

    def seeded(self):
        return Plan.objects.filter(
            code__in=[
                "starter_minutes", "growth_minutes", "scale_minutes",
                "growth_minutes_yearly",
            ]
        )

    def test_the_seed_loads(self):
        self.assertEqual(self.seeded().count(), 4)

    def test_the_seed_is_idempotent(self):
        """`load_data` is documented as safe to re-run, and a second run must not
        duplicate a plan or double its rate card."""
        for name in SEED_FILES:
            call_command("load_data", name)

        self.assertEqual(self.seeded().count(), 4)
        for plan in self.seeded():
            with self.subTest(plan=plan.code):
                self.assertEqual(
                    plan.credit_rates.filter(effective_to__isnull=True).count(), 3
                )

    def test_every_seeded_plan_is_active_not_a_draft(self):
        """`status` drives `is_active` and defaults to DRAFT, so a seed row that omits
        it loads as something nobody can buy."""
        for plan in self.seeded():
            with self.subTest(plan=plan.code):
                self.assertEqual(plan.status, Plan.Status.ACTIVE)
                self.assertTrue(plan.is_active)

    def test_every_seeded_plan_is_sold_by_the_minute(self):
        for plan in self.seeded():
            with self.subTest(plan=plan.code):
                self.assertTrue(plan.is_minutes_based)
                self.assertIsNotNone(plan.credits_per_minute())

    def test_every_seeded_plan_passes_the_activation_gate(self):
        """The whole point. A plan that cannot activate cannot be sold, and the ways
        it fails are invisible."""
        for plan in self.seeded():
            with self.subTest(plan=plan.code):
                self.assertEqual(readiness.blocking_problems(plan), [])

    def test_every_seeded_plan_has_a_full_grid(self):
        for plan in self.seeded():
            with self.subTest(plan=plan.code):
                self.assertEqual(plan.entitlements.filter(is_deleted=False).count(), 7)
                self.assertEqual(
                    plan.credit_rates.filter(effective_to__isnull=True).count(), 3
                )

    def test_each_plan_sells_credits_at_one_price(self):
        """Otherwise which price a customer gets depends on a basis tier they never
        chose."""
        for plan in self.seeded():
            with self.subTest(plan=plan.code):
                ratios = set(plan_selection.rupees_per_credit(plan).values())
                self.assertEqual(len(ratios), 1, f"{plan.code}: {ratios}")

    def test_campaigns_are_windowed(self):
        """A standing total would hand the allowance back when a campaign is deleted,
        after the calls have already gone out."""
        for plan in self.seeded():
            row = plan.entitlements.get(resource="campaign_monthly")
            with self.subTest(plan=plan.code):
                self.assertEqual(row.reset_period, "monthly")


class SeededLadderTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        for name in SEED_FILES:
            call_command("load_data", name)

    def test_the_monthly_ladder_is_contiguous(self):
        ladder = plan_selection.ladder(billing_cycle=Plan.BillingCycleChoices.MONTHLY)

        self.assertEqual(len(ladder), 3)
        self.assertEqual(plan_selection.ladder_problems(ladder), [])

    def test_the_yearly_ladder_is_clean(self):
        ladder = plan_selection.ladder(billing_cycle=Plan.BillingCycleChoices.YEARLY)

        self.assertEqual(len(ladder), 1)
        self.assertEqual(plan_selection.ladder_problems(ladder), [])

    def test_the_ladder_has_no_warnings_either(self):
        ladder = plan_selection.ladder(billing_cycle=Plan.BillingCycleChoices.MONTHLY)
        self.assertEqual(plan_selection.ladder_warnings(ladder), [])

    def test_legacy_plans_do_not_collide_with_the_seeded_ladder(self):
        """Both generations are seeded and both are active; the legacy ones must stay
        out of the ladder entirely."""
        call_command("load_data", "plan")

        ladder = plan_selection.ladder(billing_cycle=Plan.BillingCycleChoices.MONTHLY)

        self.assertEqual(plan_selection.ladder_problems(ladder), [])
        self.assertNotIn("starter_inr", [p.code for p in ladder])

    def test_every_quantity_from_one_to_the_ceiling_finds_a_plan(self):
        """The property that makes auto-selection work at all."""
        from onboard.models.plan import MAX_SELECTABLE_MINUTES

        ladder = plan_selection.ladder(billing_cycle=Plan.BillingCycleChoices.MONTHLY)
        for minutes in (1, 250, 500, 501, 1200, 2000, 2001, 50_000,
                        MAX_SELECTABLE_MINUTES):
            with self.subTest(minutes=minutes):
                plan, sold = plan_selection.for_minutes(minutes, plans=ladder)
                self.assertIsNotNone(plan, minutes)
                # A contiguous ranged ladder never rounds up.
                self.assertEqual(sold, minutes)

    def test_the_ladder_gets_cheaper_per_minute_as_it_rises(self):
        ladder = plan_selection.ladder(billing_cycle=Plan.BillingCycleChoices.MONTHLY)
        rates = [p.basis_rate().price_per_minute_inr for p in ladder]

        self.assertEqual(rates, sorted(rates, reverse=True))

    def test_a_quantity_at_the_ceiling_is_still_quotable(self):
        """The largest thing anybody can ask for must produce a price the amount
        column can hold."""
        from onboard.models.plan import MAX_CHARGEABLE_TOTAL, MAX_SELECTABLE_MINUTES

        plan, sold = plan_selection.for_minutes(MAX_SELECTABLE_MINUTES)
        quote = pricing.quote_plan(plan, sold)

        self.assertLessEqual(quote.total, MAX_CHARGEABLE_TOTAL)
        self.assertGreater(quote.total, Decimal("0.00"))
