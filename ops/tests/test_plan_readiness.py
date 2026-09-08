"""Readiness — the gate that stops a half-configured plan reaching a customer.

The two blocking cases are the reason this module exists, and both are invisible in
production: a missing allowance reads as *unlimited*, and a missing rate fails at the
point of a live call. Everything else warns.
"""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase

from onboard.constants.entitlement_resource import EntitlementResource as Resource
from onboard.constants.reset_period import ResetPeriod
from onboard.models import Plan, PlanEntitlement
from onboard.tests.billing_factories import make_plan
from ops.services.plan import readiness
from wallet.models import CreditRate
from onboard.tests.billing_factories import add_rates

Operation = CreditRate.Operation


def codes(items):
    return {item["code"] for item in items}


class BlockingTests(TestCase):
    def test_a_complete_plan_can_be_activated(self):
        plan = make_plan("complete")

        report = readiness.report(plan)

        self.assertTrue(report["can_activate"])
        self.assertEqual(report["blocking"], [])
        self.assertEqual(report["entitlements_configured"], 7)
        self.assertEqual(report["rates_configured"], 3)

    def test_a_missing_allowance_blocks_activation(self):
        """**Not a limit of zero — no limit at all.** `limit_for()` returns None and
        `check()` returns without comparing anything, so the resource is unlimited
        and nothing anywhere reports it."""
        plan = make_plan("no-storage")
        plan.entitlements.get(resource=Resource.STORAGE_GB).soft_delete()

        report = readiness.report(plan)

        self.assertFalse(report["can_activate"])
        self.assertIn("MISSING_ENTITLEMENTS", codes(report["blocking"]))
        problem = next(p for p in report["blocking"]
                       if p["code"] == "MISSING_ENTITLEMENTS")
        self.assertEqual(problem["resources"], [Resource.STORAGE_GB])
        self.assertIn("no limit at all", problem["message"])

    def test_a_missing_rate_blocks_activation(self):
        """`resolve_rate()` raises rather than charging zero, so the failure lands on
        a live call to a real customer."""
        plan = make_plan("no-realtime", with_rates=False)
        CreditRate.objects.create(
            plan=plan, operation=CreditRate.Operation.STATIC_REEL,
            credits_per_unit=2, effective_from=plan.created_at,
        )

        report = readiness.report(plan)

        self.assertFalse(report["can_activate"])
        problem = next(p for p in report["blocking"] if p["code"] == "MISSING_RATES")
        self.assertEqual(
            set(problem["operations"]),
            {CreditRate.Operation.CONVERSATION_HIGH_LATENCY,
             CreditRate.Operation.CONVERSATION_LOW_LATENCY},
        )

    def test_a_superseded_rate_does_not_count_as_configured(self):
        """Only the open row prices calls. A closed one is history."""
        plan = make_plan("superseded")
        rate = plan.credit_rates.filter(
            operation=CreditRate.Operation.STATIC_REEL, effective_to__isnull=True
        ).first()
        # The constraint requires effective_to > effective_from, so a closed row
        # cannot end at the instant it began.
        rate.effective_to = rate.effective_from + timedelta(seconds=1)
        rate.save(update_fields=["effective_to"])

        report = readiness.report(plan)

        self.assertFalse(report["can_activate"])
        self.assertIn("MISSING_RATES", codes(report["blocking"]))

    def test_a_soft_deleted_allowance_does_not_count(self):
        plan = make_plan("soft-deleted-row")
        row = plan.entitlements.get(resource=Resource.AGENT)
        row.soft_delete()

        self.assertFalse(readiness.report(plan)["can_activate"])

    def test_warnings_never_block(self):
        """Every warning is a legitimate configuration somewhere. A rule refusing
        them would be wrong more often than the mistake it prevents."""
        plan = make_plan("warned", sort_order=0)

        report = readiness.report(plan)

        self.assertTrue(report["can_activate"])
        self.assertIn("NO_SORT_ORDER", codes(report["warnings"]))


class WarningTests(TestCase):
    def test_a_discount_over_100_is_flagged(self):
        plan = make_plan("over-discounted", discount_percentage=Decimal("150.00"))

        self.assertIn("DISCOUNT_OVER_100", codes(readiness.warnings(plan)))

    def test_a_plan_whose_basis_grants_no_credits_is_refused_outright(self):
        """This was a warning while a plan could grant a fixed number of credits. Now
        the grant is minutes x the basis rate, so a basis of zero means the plan can
        never grant anything at all -- which is a refusal, not a note."""
        plan = make_plan("no-credits", with_rates=False)
        add_rates(plan, {op: (0, Decimal("1.0000")) for op in Operation.values})

        self.assertEqual(plan.credits_for(plan.min_minutes), 0)

    def test_disabled_top_ups_on_a_paid_plan_are_flagged(self):
        plan = make_plan("no-topups", cost_per_additional_credit=Decimal("0.00"))

        self.assertIn("TOP_UPS_DISABLED", codes(readiness.warnings(plan)))

    def test_a_free_plan_is_not_nagged_about_credits_or_top_ups(self):
        """These warnings are about a plan being charged for and giving nothing
        back. A free plan giving nothing back is the arrangement."""
        plan = make_plan(
            "free", base_price=Decimal("0.00"), allocated_credits=0,
            cost_per_additional_credit=Decimal("0.00"),
        )

        found = codes(readiness.warnings(plan))

        self.assertNotIn("PAID_PLAN_NO_CREDITS", found)
        self.assertNotIn("TOP_UPS_DISABLED", found)

    def test_capacity_sold_for_nothing_is_flagged(self):
        plan = make_plan("free-storage")
        plan.entitlements.filter(resource=Resource.STORAGE_GB).update(
            is_addon_available=True, price_per_additional=Decimal("0.00"),
            included_quantity=5,
        )

        found = [w for w in readiness.warnings(plan)
                 if w["code"] == "ADDON_PRICED_AT_ZERO"]

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["resource"], Resource.STORAGE_GB)

    def test_an_uncapped_addon_is_flagged(self):
        """The cap is what stops a subscriber assembling the plan above at add-on
        prices."""
        plan = make_plan("uncapped")
        plan.entitlements.filter(resource=Resource.AGENT).update(
            is_addon_available=True, price_per_additional=Decimal("199.00"),
            included_quantity=2, max_additional=None,
        )

        self.assertIn("ADDON_UNCAPPED", codes(readiness.warnings(plan)))

    def test_campaigns_left_as_a_standing_total_are_flagged(self):
        """`none` makes the allowance a holding rather than an act, so deleting a
        campaign hands it back even after the calls went out."""
        plan = make_plan("campaign-standing")
        plan.entitlements.filter(resource=Resource.CAMPAIGN_MONTHLY).update(
            reset_period=ResetPeriod.NONE
        )

        self.assertIn("CAMPAIGN_NOT_WINDOWED", codes(readiness.warnings(plan)))

    def test_campaigns_sold_as_an_addon_are_flagged(self):
        plan = make_plan("campaign-sold")
        plan.entitlements.filter(resource=Resource.CAMPAIGN_MONTHLY).update(
            is_addon_available=True, price_per_additional=Decimal("150.00"),
            included_quantity=5,
        )

        self.assertIn("CAMPAIGNS_SOLD_AS_ADDON", codes(readiness.warnings(plan)))

    def test_a_free_call_tier_on_a_paid_plan_is_flagged(self):
        plan = make_plan("free-realtime", rates={
            CreditRate.Operation.CONVERSATION_LOW_LATENCY: 0,
        })

        found = [w for w in readiness.warnings(plan)
                 if w["code"] == "TIER_PRICED_AT_ZERO"]

        self.assertEqual(len(found), 1)
        self.assertIn(
            CreditRate.Operation.CONVERSATION_LOW_LATENCY, found[0]["operations"]
        )

    def test_a_clean_plan_produces_no_warnings(self):
        plan = make_plan(
            "clean", sort_order=3, allocated_credits=1000,
            cost_per_additional_credit=Decimal("1.20"),
        )
        # The factory derives `is_addon_available` from `max_additional`, so its
        # campaign row is purchasable -- which the warning above objects to, rightly.
        # A genuinely clean plan has campaigns off.
        plan.entitlements.filter(resource=Resource.CAMPAIGN_MONTHLY).update(
            is_addon_available=False
        )

        self.assertEqual(readiness.warnings(plan), [])


class YearlyPricingWarningTests(TestCase):
    """The mispricing already documented for commercial: block prices are charged
    per cycle, so a yearly plan carrying monthly figures sells the same capacity for
    a twelfth of the price."""

    def setUp(self):
        self.monthly = make_plan("yr-monthly")
        PlanEntitlement.objects.filter(
            plan=self.monthly, resource=Resource.STORAGE_GB
        ).update(
            is_addon_available=True, price_per_additional=Decimal("199.00"),
            included_quantity=10, addon_step=10, max_additional=90,
        )

    def test_a_yearly_plan_copying_monthly_prices_is_flagged(self):
        yearly = make_plan(
            "yr-yearly", billing_cycle=Plan.BillingCycleChoices.YEARLY
        )
        PlanEntitlement.objects.filter(
            plan=yearly, resource=Resource.STORAGE_GB
        ).update(
            is_addon_available=True, price_per_additional=Decimal("199.00"),
            included_quantity=10, addon_step=10, max_additional=90,
        )

        self.assertIn(
            "YEARLY_ADDONS_LOOK_MONTHLY", codes(readiness.warnings(yearly))
        )

    def test_a_yearly_plan_with_its_own_prices_is_not_flagged(self):
        yearly = make_plan(
            "yr-priced", billing_cycle=Plan.BillingCycleChoices.YEARLY
        )
        PlanEntitlement.objects.filter(
            plan=yearly, resource=Resource.STORAGE_GB
        ).update(
            is_addon_available=True, price_per_additional=Decimal("2388.00"),
            included_quantity=10, addon_step=10, max_additional=90,
        )

        self.assertNotIn(
            "YEARLY_ADDONS_LOOK_MONTHLY", codes(readiness.warnings(yearly))
        )

    def test_a_monthly_plan_is_never_flagged_for_this(self):
        self.assertNotIn(
            "YEARLY_ADDONS_LOOK_MONTHLY", codes(readiness.warnings(self.monthly))
        )
