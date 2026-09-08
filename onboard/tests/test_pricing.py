"""Section 10 of the billing test plan — PR-01 to PR-50.

Every number a tenant is asked to pay mid-cycle. These are the figures that end up on
an invoice and get disputed months later, so they are asserted to the paisa rather
than to a tolerance — and every quote is computed against a **fixed** ``now`` so a
failure means the arithmetic changed, not that the clock moved.

The load-bearing idea, tested throughout §10.4: **the sign of the result decides what
kind of change this is.** Owing money is an upgrade, applied when the payment clears;
owing nothing is a downgrade, applied at the end of the cycle already paid for. That
falls out correctly for monthly-to-yearly, yearly-to-monthly and same-price sidegrades
without anyone having to rank the plans.
"""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from onboard.constants.entitlement_resource import EntitlementResource
from onboard.models import Plan, Subscription, TenantEntitlement
from onboard.services import pricing
from onboard.services import subscriptions as subscription_service
from onboard.tests.billing_factories import (
    add_entitlements,
    make_free_plan,
    add_rates,
    make_plan,
    make_tenant,
)
from wallet.models import CreditLedger
from wallet.services import wallet as wallet_service

Bucket = CreditLedger.Bucket
EntryType = CreditLedger.EntryType
Resource = EntitlementResource
Status = Subscription.SubscriptionStatusChoices

#: Ratio 0.3333 — twenty days into a thirty-day cycle. Reused across §10.3 and §10.4
#: so the prorated figures in both are checkable by hand against the same fraction.
DAYS_ELAPSED = 20
DAYS_TOTAL = 30


class PricingTestCase(TestCase):
    """A tenant twenty days into a thirty-day cycle on a ₹999 plan."""

    def setUp(self):
        self.ctx = make_tenant("pricing")
        self.tenant = self.ctx.tenant
        self.now = timezone.now()

        self.starter = make_plan(
            "pricing-starter",
            base_price=Decimal("999.00"),
            tax_percentage=Decimal("18.00"),
            allocated_credits=1000,
            cost_per_additional_credit=Decimal("1.20"),
        )
        self.professional = make_plan(
            "pricing-professional",
            base_price=Decimal("2699.10"),
            tax_percentage=Decimal("18.00"),
            allocated_credits=3500,
            cost_per_additional_credit=Decimal("1.00"),
        )
        # Cheap enough that a third of the Starter cycle (Rs 332.97) covers it
        # outright, which is what makes a move to it owe nothing at all.
        self.much_cheaper = make_plan(
            "pricing-much-cheaper",
            base_price=Decimal("300.00"),
            tax_percentage=Decimal("18.00"),
        )
        self.subscription = subscription_service.activate(self.tenant, self.starter)
        self.set_cycle(DAYS_ELAPSED, DAYS_TOTAL)

    def set_cycle(self, days_elapsed, days_total, subscription=None):
        """Place the cycle window around ``self.now`` exactly.

        ``activate()`` produces a calendar month, which is 28 to 31 days depending on
        when the suite runs. Pinning the window is what lets these tests assert
        332.97 rather than "about a third".
        """
        subscription = subscription or self.subscription
        Subscription.objects.filter(pk=subscription.pk).update(
            start_date=self.now - timedelta(days=days_elapsed),
            end_date=self.now + timedelta(days=days_total - days_elapsed),
        )
        subscription.refresh_from_db()
        return subscription


class UnusedRatioTests(PricingTestCase):
    """PR-01 to PR-04 — how much of the cycle is still to come."""

    def test_pr01_a_brand_new_cycle_is_entirely_unused(self):
        """PR-01."""
        self.set_cycle(0, DAYS_TOTAL)

        self.assertEqual(
            pricing.unused_ratio(self.subscription, now=self.now), Decimal("1.0000")
        )

    def test_pr02_two_thirds_through_leaves_a_third(self):
        """PR-02: the fraction every prorated figure in this file is built on."""
        self.assertEqual(
            pricing.unused_ratio(self.subscription, now=self.now), Decimal("0.3333")
        )

    def test_pr03_a_finished_cycle_has_nothing_left_to_credit(self):
        """PR-03: never negative — a lapsed cycle owes the tenant nothing back."""
        self.set_cycle(45, DAYS_TOTAL)

        self.assertEqual(
            pricing.unused_ratio(self.subscription, now=self.now), Decimal("0")
        )

    def test_pr04_a_zero_length_cycle_does_not_divide_by_zero(self):
        """PR-04: a corrupt window must produce a conservative zero, not a 500."""
        Subscription.objects.filter(pk=self.subscription.pk).update(
            start_date=self.now, end_date=self.now
        )
        self.subscription.refresh_from_db()

        self.assertEqual(
            pricing.unused_ratio(self.subscription, now=self.now), Decimal("0")
        )


class TopUpQuoteTests(PricingTestCase):
    """PR-05 to PR-13 — buying credits mid-cycle."""

    def test_pr05_credits_are_priced_at_the_plans_rate(self):
        """PR-05: 500 × ₹1.20."""
        quote = pricing.quote_top_up(self.subscription, 500)

        self.assertEqual(quote.net, Decimal("600.00"))

    def test_pr06_tax_is_added_at_the_plans_rate(self):
        """PR-06."""
        quote = pricing.quote_top_up(self.subscription, 500)

        self.assertEqual(quote.tax, Decimal("108.00"))
        self.assertEqual(quote.total, Decimal("708.00"))
        self.assertEqual(quote.tax_percentage, Decimal("18.00"))

    def test_pr07_a_credit_costs_the_same_whenever_it_is_bought(self):
        """PR-07: **not prorated, deliberately.**

        A credit never expires out of the purchased bucket, so there is no partial
        cycle to discount — unlike capacity, which is rented for the remainder.
        """
        self.set_cycle(1, DAYS_TOTAL)
        early = pricing.quote_top_up(self.subscription, 500)

        self.set_cycle(29, DAYS_TOTAL)
        late = pricing.quote_top_up(self.subscription, 500)

        self.assertEqual(early.total, late.total)

    def test_pr08_a_plan_that_does_not_sell_credits_refuses_to_quote(self):
        """PR-08: upgrading is the way to get more, and saying so beats quoting
        ₹0.00 for something the tenant will never receive."""
        no_top_ups = make_plan(
            "pricing-no-top-ups", cost_per_additional_credit=Decimal("0.00")
        )
        subscription = subscription_service.activate(
            make_tenant("pricing-no-top-ups-tenant"), no_top_ups
        )

        with self.assertRaises(pricing.PricingError):
            pricing.quote_top_up(subscription, 500)

    def test_pr09_zero_credits_is_not_a_purchase(self):
        """PR-09."""
        with self.assertRaises(pricing.PricingError):
            pricing.quote_top_up(self.subscription, 0)

    def test_pr10_a_negative_quantity_is_refused(self):
        """PR-10: otherwise a negative order would invoice a negative amount."""
        with self.assertRaises(pricing.PricingError):
            pricing.quote_top_up(self.subscription, -500)

    def test_pr11_an_absurd_quantity_is_refused(self):
        """PR-11: not a policy so much as a guard — a mistyped quantity should be
        refused, not charged."""
        with self.assertRaises(pricing.PricingError) as caught:
            pricing.quote_top_up(
                self.subscription, pricing.MAX_TOP_UP_CREDITS + 1
            )

        self.assertIn("limited to", str(caught.exception))

    def test_pr12_money_rounds_half_up(self):
        """PR-12: ₹111.925 of tax bills as ₹111.93.

        Python's default is half-*even*, which would bill ₹111.92 and make the
        invoice disagree with every calculator a customer checks it on.
        """
        plan = make_plan(
            "pricing-half-up",
            base_price=Decimal("999.00"),
            tax_percentage=Decimal("18.50"),
            cost_per_additional_credit=Decimal("1.21"),
        )
        subscription = subscription_service.activate(
            make_tenant("pricing-half-up-tenant"), plan
        )

        quote = pricing.quote_top_up(subscription, 500)

        self.assertEqual(quote.net, Decimal("605.00"))
        self.assertEqual(quote.tax, Decimal("111.93"))

    def test_pr13_the_breakdown_records_how_the_price_was_reached(self):
        """PR-13: stored verbatim on the transaction, because the plan's price may
        have moved by the time anyone asks."""
        breakdown = pricing.quote_top_up(self.subscription, 500).as_breakdown()

        self.assertEqual(breakdown["credits"], "500")
        self.assertEqual(breakdown["unit_price"], "1.20")
        self.assertEqual(breakdown["total"], "708.00")


class AddonQuoteTests(PricingTestCase):
    """PR-14 to PR-24 — renting extra capacity for the rest of the cycle."""

    def setUp(self):
        super().setUp()
        # 10 GB blocks at ₹199, up to 90 GB extra.
        self.storage = self.starter.entitlements.get(resource=Resource.STORAGE_GB)
        self.storage.included_quantity = 10
        self.storage.price_per_additional = Decimal("199.00")
        self.storage.addon_step = 10
        self.storage.max_additional = 90
        self.storage.is_addon_available = True
        self.storage.save()

    def hold(self, quantity, resource=Resource.STORAGE_GB):
        return TenantEntitlement.objects.create(
            tenant=self.tenant,
            resource=resource,
            additional_quantity=quantity,
            price_per_unit=Decimal("199.00"),
            addon_step=10,
            subscription=self.subscription,
        )

    def test_pr14_capacity_is_priced_per_whole_block(self):
        """PR-14: 20 GB on a 10 GB step is two blocks."""
        quote = pricing.quote_addon(
            self.subscription, Resource.STORAGE_GB, 20, now=self.now
        )

        self.assertEqual(quote.detail["recurring_net"], Decimal("398.00"))

    def test_pr15_a_partial_block_is_rounded_up(self):
        """PR-15: billing a fraction of a block would mean either giving away the
        remainder or invoicing a number the tenant cannot reconcile."""
        quote = pricing.quote_addon(
            self.subscription, Resource.STORAGE_GB, 12, now=self.now
        )

        self.assertEqual(quote.detail["recurring_net"], Decimal("398.00"))

    def test_pr16_the_charge_now_is_prorated_to_the_rest_of_the_cycle(self):
        """PR-16: ₹398.00 × 0.3333 = ₹132.65."""
        quote = pricing.quote_addon(
            self.subscription, Resource.STORAGE_GB, 20, now=self.now
        )

        self.assertEqual(quote.detail["unused_ratio"], Decimal("0.3333"))
        self.assertEqual(quote.net, Decimal("132.65"))

    def test_pr17_both_the_charge_now_and_the_ongoing_cost_are_reported(self):
        """PR-17: **showing only the prorated figure understates the commitment.**

        A tenant told "₹156.53" who then sees ₹469.64 on every renewal was not
        given the price of what they bought.
        """
        quote = pricing.quote_addon(
            self.subscription, Resource.STORAGE_GB, 20, now=self.now
        )

        self.assertEqual(quote.detail["recurring_net"], Decimal("398.00"))
        self.assertEqual(quote.total, Decimal("156.53"))
        self.assertNotEqual(quote.total, quote.detail["recurring_net"])

    def test_pr18_buying_in_two_steps_costs_the_same_as_buying_at_once(self):
        """PR-18: **the delta pricing cannot be gamed.**

        The charge is (price of the new total) − (price of what they already pay
        for), so ten then ten is exactly twenty.
        """
        at_once = pricing.quote_addon(
            self.subscription, Resource.STORAGE_GB, 20, now=self.now
        ).detail["added_recurring_net"]

        first = pricing.quote_addon(
            self.subscription, Resource.STORAGE_GB, 10, now=self.now
        ).detail["added_recurring_net"]
        self.hold(10)
        second = pricing.quote_addon(
            self.subscription, Resource.STORAGE_GB, 10, now=self.now
        ).detail["added_recurring_net"]

        self.assertEqual(first + second, at_once)

    def test_pr19_going_past_the_ceiling_is_refused_and_says_where_it_is(self):
        """PR-19: an error that does not name the limit sends the tenant to
        support to ask what it is."""
        with self.assertRaises(pricing.PricingError) as caught:
            pricing.quote_addon(
                self.subscription, Resource.STORAGE_GB, 100, now=self.now
            )

        self.assertIn("90", str(caught.exception))

    def test_pr20_capacity_already_held_counts_toward_the_ceiling(self):
        """PR-20: **otherwise the cap is per-order rather than per-tenant** and can
        be walked past in ten-gigabyte steps."""
        self.hold(80)

        with self.assertRaises(pricing.PricingError) as caught:
            pricing.quote_addon(
                self.subscription, Resource.STORAGE_GB, 20, now=self.now
            )

        self.assertIn("80", str(caught.exception))

    def test_pr21_a_resource_that_cannot_be_extended_is_refused(self):
        """PR-21."""
        self.storage.is_addon_available = False
        self.storage.save(update_fields=["is_addon_available"])

        with self.assertRaises(pricing.PricingError) as caught:
            pricing.quote_addon(
                self.subscription, Resource.STORAGE_GB, 10, now=self.now
            )

        self.assertIn("Upgrading", str(caught.exception))

    def test_pr22_a_resource_the_plan_never_configured_is_refused(self):
        """PR-22: 'no allowance configured' is a plan-configuration problem, and
        pricing it at zero would hide it."""
        bare_plan = make_plan("pricing-bare", with_entitlements=False)
        add_entitlements(bare_plan, {Resource.AGENT: (2, Decimal("500.00"), 1, 10)})
        subscription = subscription_service.activate(
            make_tenant("pricing-bare-tenant"), bare_plan
        )

        with self.assertRaises(pricing.PricingError) as caught:
            pricing.quote_addon(subscription, Resource.STORAGE_GB, 10, now=self.now)

        self.assertIn("allowance", str(caught.exception))

    def test_pr23_a_non_positive_quantity_is_refused(self):
        """PR-23."""
        for quantity in (0, -10):
            with self.assertRaises(pricing.PricingError):
                pricing.quote_addon(
                    self.subscription, Resource.STORAGE_GB, quantity, now=self.now
                )

    def test_pr24_add_ons_are_priced_from_todays_catalogue(self):
        """PR-24: **an add-on is a new agreement made today**, unlike the plan
        price, which was agreed when the tenant subscribed. Today's price is the
        one being offered — and it is then frozen onto the tenant's own row."""
        self.storage.price_per_additional = Decimal("299.00")
        self.storage.save(update_fields=["price_per_additional"])

        quote = pricing.quote_addon(
            self.subscription, Resource.STORAGE_GB, 20, now=self.now
        )

        self.assertEqual(quote.detail["recurring_net"], Decimal("598.00"))


class PlanChangeQuoteTests(PricingTestCase):
    """PR-25 to PR-36 — moving between plans mid-cycle."""

    def quote(self, new_plan, subscription=None):
        return pricing.quote_plan_change(
            subscription or self.subscription, new_plan, now=self.now
        )

    def test_pr25_an_upgrade_buys_extra_days_not_a_discount(self):
        """PR-25: **the worked example.**

        ₹999.00 × 0.3333 = ₹332.97 of unused time. That is *not* taken off the new
        plan — the tenant pays Professional's full ₹2699.10 + 18% = ₹3184.94, and
        the ₹332.97 buys days at Professional's own daily rate instead.

        The discount model reduced the taxable value of one supply using time already
        taxed on another, and produced an invoice matching no published price. This
        way every upgrade invoices at the plan's list price.
        """
        quote = self.quote(self.professional)

        self.assertEqual(quote.detail["credit_from_current"], Decimal("332.97"))
        self.assertEqual(quote.detail["bonus_value"], Decimal("332.97"))
        self.assertEqual(quote.net, Decimal("2699.10"))
        self.assertEqual(quote.tax, Decimal("485.84"))
        self.assertEqual(quote.total, Decimal("3184.94"))

        # ₹2699.10 over a calendar month, and ₹332.97 buys that many days of it.
        per_day = Decimal("2699.10") / Decimal(str(quote.detail["new_plan_cycle_days"]))
        self.assertEqual(
            quote.detail["bonus_days"],
            (Decimal("332.97") / per_day).quantize(Decimal("0.01")),
        )

    def test_pr26_owing_money_makes_it_a_paid_change(self):
        """PR-26."""
        self.assertEqual(
            self.quote(self.professional).detail["change_type"], pricing.PAID_CHANGE
        )

    def test_pr27_owing_nothing_makes_it_a_scheduled_change(self):
        """PR-27: **never a refund.** The tenant keeps what they paid for until the
        cycle ends, and the cheaper plan starts after that."""
        quote = self.quote(self.much_cheaper)

        self.assertEqual(quote.detail["change_type"], pricing.SCHEDULED_CHANGE)
        self.assertEqual(quote.total, Decimal("0.00"))

        # And "nothing owed" really is the test, not "the plan is cheaper": a plan
        # priced above the credit still owes the difference and applies now.
        mid_priced = make_plan(
            "pricing-mid",
            base_price=Decimal("500.00"),
            tax_percentage=Decimal("18.00"),
        )
        self.assertEqual(
            self.quote(mid_priced).detail["change_type"], pricing.PAID_CHANGE
        )

    def test_pr28_a_sub_rupee_difference_is_applied_without_a_payment(self):
        """PR-28: below the gateway minimum, charging is impossible and refusing is
        absurd for a change the tenant wants."""
        free_tenant = make_tenant("pricing-sub-rupee")
        free = make_free_plan("pricing-sub-rupee-free")
        subscription = subscription_service.activate(free_tenant, free)
        self.set_cycle(DAYS_ELAPSED, DAYS_TOTAL, subscription)
        nearly_free = make_plan(
            "pricing-nearly-free",
            base_price=Decimal("0.50"),
            tax_percentage=Decimal("18.00"),
        )

        quote = self.quote(nearly_free, subscription)

        self.assertEqual(quote.detail["change_type"], pricing.IMMEDIATE_FREE)
        self.assertLess(quote.total, Decimal("1.00"))

    def test_pr29_tax_is_charged_at_the_new_plans_rate(self):
        """PR-29: **on the actual taxable amount**, not on a figure that already
        includes somebody else's tax. The whole calculation runs pre-tax for this
        reason."""
        different_tax = make_plan(
            "pricing-different-tax",
            base_price=Decimal("2699.10"),
            tax_percentage=Decimal("5.00"),
        )

        quote = self.quote(different_tax)

        # Charged on the new plan's full net price, at the new plan's rate. The
        # unused time never touches the taxable value now -- it is settled in days.
        self.assertEqual(quote.tax_percentage, Decimal("5.00"))
        self.assertEqual(quote.net, Decimal("2699.10"))
        self.assertEqual(quote.tax, Decimal("134.96"))

    def test_pr30_the_credit_is_based_on_what_the_tenant_actually_paid(self):
        """PR-30: **the snapshot, not the live plan.**

        Raising the Starter price must not retroactively increase the credit owed
        to somebody who bought it cheaper.
        """
        # Re-pricing is a supersede now, not a column edit. Same intent: the plan
        # gets dearer after the tenant bought it.
        current = self.starter.basis_rate()
        current.effective_to = timezone.now()
        current.save(update_fields=["effective_to"])
        add_rates(
            self.starter,
            {self.starter.credit_basis: (
                current.credits_per_unit,
                current.price_per_minute_inr * 5,
            )},
        )

        quote = self.quote(self.professional)

        self.assertEqual(quote.detail["credit_from_current"], Decimal("332.97"))

    def test_pr31_unused_add_on_capacity_is_credited_too(self):
        """PR-31: add-ons follow the tenant across a plan change and are re-charged
        each cycle, so the unused part of what they already paid is theirs."""
        TenantEntitlement.objects.create(
            tenant=self.tenant,
            resource=Resource.STORAGE_GB,
            additional_quantity=20,
            price_per_unit=Decimal("199.00"),
            addon_step=10,
            subscription=self.subscription,
        )

        quote = self.quote(self.professional)

        self.assertEqual(quote.detail["credit_from_addons"], Decimal("132.65"))
        self.assertEqual(quote.detail["taxable_difference"], Decimal("2233.48"))

    def test_pr32_changing_to_the_current_plan_is_not_a_change(self):
        """PR-32."""
        with self.assertRaises(pricing.PricingError):
            self.quote(self.starter)

    def test_pr33_monthly_to_yearly_is_priced_by_the_money_not_the_tier(self):
        """PR-33: **there is no tier ladder anywhere in this system**, which is why
        this works without one."""
        yearly = make_plan(
            "pricing-yearly",
            base_price=Decimal("9990.00"),
            tax_percentage=Decimal("18.00"),
            billing_cycle=Plan.BillingCycleChoices.YEARLY,
        )

        quote = self.quote(yearly)

        self.assertEqual(quote.detail["change_type"], pricing.PAID_CHANGE)
        self.assertEqual(quote.detail["credit_from_current"], Decimal("332.97"))

    def test_pr34_yearly_to_monthly_is_scheduled_rather_than_refunded(self):
        """PR-34: a year of unused time is a large credit, and it must not become a
        payout."""
        yearly_tenant = make_tenant("pricing-yearly-tenant")
        yearly = make_plan(
            "pricing-yearly-source",
            base_price=Decimal("9990.00"),
            tax_percentage=Decimal("18.00"),
            billing_cycle=Plan.BillingCycleChoices.YEARLY,
        )
        subscription = subscription_service.activate(yearly_tenant, yearly)
        self.set_cycle(20, 365, subscription)

        quote = self.quote(self.starter, subscription)

        self.assertEqual(quote.detail["change_type"], pricing.SCHEDULED_CHANGE)
        self.assertEqual(quote.total, Decimal("0.00"))

    def test_pr35_a_same_price_sidegrade_is_decided_by_money_like_everything_else(self):
        """PR-35: **there is no special case for "same price".**

        A plan change starts a fresh cycle, so the question is always the same one:
        does the credit for unused time cover the new plan? On day 0 of a cycle it
        does exactly, and the move waits for the cycle to end. Twenty days in it
        covers a third, and the tenant owes the two-thirds they have used — the same
        arithmetic an upgrade gets, and the same total per day either way.
        """
        sidegrade = make_plan(
            "pricing-sidegrade",
            base_price=Decimal("999.00"),
            tax_percentage=Decimal("18.00"),
        )

        self.set_cycle(0, DAYS_TOTAL)
        untouched_cycle = self.quote(sidegrade)
        self.assertEqual(
            untouched_cycle.detail["change_type"], pricing.SCHEDULED_CHANGE
        )
        self.assertEqual(untouched_cycle.total, Decimal("0.00"))

        self.set_cycle(DAYS_ELAPSED, DAYS_TOTAL)
        mid_cycle = self.quote(sidegrade)
        self.assertEqual(mid_cycle.detail["change_type"], pricing.PAID_CHANGE)
        # Full list price, with the third of a cycle they have not used buying back
        # roughly a third of a cycle in days -- same price, so same rate either side.
        self.assertEqual(mid_cycle.net, Decimal("999.00"))
        self.assertEqual(mid_cycle.detail["bonus_value"], Decimal("332.97"))
        self.assertGreater(mid_cycle.detail["bonus_days"], Decimal("0"))

    def test_pr36_effective_at_matches_the_kind_of_change(self):
        """PR-36: a paid change lands when the money does; a scheduled one waits
        for the cycle the tenant already paid for to finish."""
        paid = self.quote(self.professional)
        scheduled = self.quote(self.much_cheaper)

        self.assertEqual(paid.detail["effective_at"], self.now.isoformat())
        self.assertEqual(
            scheduled.detail["effective_at"], self.subscription.end_date.isoformat()
        )


class CreditImpactTests(PricingTestCase):
    """PR-37 to PR-40 — what a plan change does to the balance."""

    def impact(self, plan, subscription, reason="upgrade"):
        return pricing.credit_impact(subscription, plan, reason=reason)

    def subscribe(self, name, **plan_overrides):
        tenant = make_tenant(name)
        plan = make_plan(f"{name}-plan", **plan_overrides)
        return tenant, subscription_service.activate(tenant, plan)

    def test_pr37_a_rolling_plan_carries_everything_and_warns_about_nothing(self):
        """PR-37."""
        tenant, subscription = self.subscribe(
            "impact-rolling",
            allocated_credits=1000,
            allow_rollover_on_upgrade=True,
            max_rollover_credits=None,
        )

        impact = self.impact(self.professional, subscription)

        self.assertEqual(impact["carried_forward"], 1000)
        self.assertEqual(impact["forfeited"], 0)
        self.assertEqual(impact["warnings"], [])

    def test_pr38_a_tenant_about_to_lose_credits_is_told_how_many(self):
        """PR-38: **before they click, not after.** 'Some credits may be lost' is
        not something anybody can make a decision on."""
        tenant, subscription = self.subscribe(
            "impact-forfeit",
            allocated_credits=1000,
            allow_rollover_on_upgrade=False,
        )

        impact = self.impact(self.professional, subscription)

        self.assertEqual(impact["carried_forward"], 0)
        self.assertEqual(impact["forfeited"], 1000)
        self.assertEqual(len(impact["warnings"]), 1)
        self.assertIn("1000", impact["warnings"][0])

    def test_pr39_hitting_the_cap_says_so_in_its_own_words(self):
        """PR-39: 'your plan does not roll over' and 'you hit the cap' are two
        different conversations with a customer."""
        tenant, subscription = self.subscribe(
            "impact-capped",
            allocated_credits=1000,
            allow_rollover_on_upgrade=True,
            max_rollover_credits=400,
        )

        impact = self.impact(self.professional, subscription)

        self.assertEqual(impact["carried_forward"], 400)
        self.assertEqual(impact["forfeited"], 600)
        self.assertIn("cap", impact["warnings"][0].lower())

    def test_pr40_the_projected_balance_adds_up(self):
        """PR-40: carried + purchased + the new plan's allocation."""
        tenant, subscription = self.subscribe(
            "impact-balance",
            allocated_credits=1000,
            allow_rollover_on_upgrade=True,
            max_rollover_credits=None,
        )
        wallet_service.grant(
            tenant.tenant, entry_type=EntryType.MANUAL_TOP_UP, amount=250,
            bucket=Bucket.PURCHASED, reference_id="impact-top-up",
        )

        impact = self.impact(self.professional, subscription)

        self.assertEqual(impact["purchased"], 250)
        self.assertEqual(impact["new_plan_allocation"], 3500)
        self.assertEqual(impact["balance_after_change"], 1000 + 250 + 3500)


class ReactivationQuoteTests(PricingTestCase):
    """PR-41 to PR-50 — quoting a lapsed subscription.

    Once a cycle is over there is no unused time to credit and no cycle to schedule
    against, so "upgrade" and "downgrade" stop being meaningful and every option
    becomes a full-price purchase.
    """

    def lapse(self, subscription=None):
        subscription = subscription or self.subscription
        Subscription.objects.filter(pk=subscription.pk).update(
            start_date=self.now - timedelta(days=DAYS_TOTAL + 2),
            end_date=self.now - timedelta(days=2),
        )
        subscription.refresh_from_db()
        subscription_service.expire(subscription)
        subscription.refresh_from_db()
        return subscription

    def quote(self, new_plan, subscription=None):
        return pricing.quote_plan_change(
            subscription or self.subscription, new_plan, now=self.now
        )

    def test_pr41_a_lapsed_quote_is_flagged_as_a_reactivation(self):
        """PR-41: the client needs to know it is not being shown a proration."""
        self.lapse()

        self.assertTrue(self.quote(self.professional).detail["reactivation"])

    def test_pr42_a_finished_cycle_credits_nothing(self):
        """PR-42."""
        self.lapse()

        self.assertEqual(
            self.quote(self.professional).detail["credit_from_current"],
            Decimal("0.00"),
        )

    def test_pr43_the_full_price_of_the_chosen_plan_is_due(self):
        """PR-43."""
        self.lapse()

        quote = self.quote(self.professional)

        self.assertEqual(quote.net, self.professional.net_base_price)
        self.assertEqual(quote.total, self.professional.total_price)

    def test_pr44_returning_on_a_cheaper_plan_is_still_a_purchase(self):
        """PR-44: **there is no cycle to schedule into.**

        Scheduling it would set an effective date in the past and leave the tenant
        expired, having chosen a plan and been charged nothing.
        """
        self.lapse()
        cheaper = make_plan(
            "pricing-reactivate-cheaper",
            base_price=Decimal("500.00"),
            tax_percentage=Decimal("18.00"),
        )

        quote = self.quote(cheaper)

        self.assertEqual(quote.detail["change_type"], pricing.PAID_CHANGE)
        self.assertNotEqual(quote.detail["change_type"], pricing.SCHEDULED_CHANGE)
        self.assertEqual(quote.total, Decimal("590.00"))

    def test_pr45_returning_on_the_free_plan_takes_effect_immediately(self):
        """PR-45."""
        self.lapse()
        free = make_free_plan("pricing-reactivate-free")

        quote = self.quote(free)

        self.assertEqual(quote.detail["change_type"], pricing.IMMEDIATE_FREE)
        self.assertEqual(quote.total, Decimal("0.00"))

    def test_pr46_a_reactivation_takes_effect_now_not_at_the_lapsed_date(self):
        """PR-46."""
        subscription = self.lapse()

        quote = self.quote(self.professional)

        self.assertEqual(quote.detail["effective_at"], self.now.isoformat())
        self.assertGreater(
            quote.detail["effective_at"], subscription.end_date.isoformat()
        )

    def test_pr47_the_date_decides_not_only_the_status(self):
        """PR-47: a subscription past its end date that the expiry job has not
        reached yet is still lapsed, and quoting it as a live cycle would credit
        time the tenant no longer has."""
        Subscription.objects.filter(pk=self.subscription.pk).update(
            start_date=self.now - timedelta(days=DAYS_TOTAL + 2),
            end_date=self.now - timedelta(days=2),
            status=Status.ACTIVE,
        )
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, Status.ACTIVE)

        quote = self.quote(self.professional)

        self.assertTrue(quote.detail["reactivation"])
        self.assertEqual(quote.detail["credit_from_current"], Decimal("0.00"))

    def test_pr48_a_dead_cycle_credits_no_add_on_time_either(self):
        """PR-48."""
        TenantEntitlement.objects.create(
            tenant=self.tenant,
            resource=Resource.STORAGE_GB,
            additional_quantity=20,
            price_per_unit=Decimal("199.00"),
            addon_step=10,
            subscription=self.subscription,
        )
        self.lapse()

        self.assertEqual(
            self.quote(self.professional).detail["credit_from_addons"],
            Decimal("0.00"),
        )

    def test_pr49_an_active_tenants_downgrade_is_untouched_by_any_of_this(self):
        """PR-49: **the regression guard.** Opening reactivation up must not turn
        every downgrade into a full-price purchase."""
        quote = self.quote(self.much_cheaper)

        self.assertFalse(quote.detail["reactivation"])
        self.assertEqual(quote.detail["change_type"], pricing.SCHEDULED_CHANGE)
        self.assertEqual(quote.total, Decimal("0.00"))

    def test_pr50_an_active_tenants_upgrade_still_prorates(self):
        """PR-50."""
        quote = self.quote(self.professional)

        self.assertFalse(quote.detail["reactivation"])
        self.assertEqual(quote.detail["credit_from_current"], Decimal("332.97"))
        self.assertEqual(quote.total, Decimal("3184.94"))
        self.assertGreater(quote.detail["bonus_days"], Decimal("0"))


# --------------------------------------------------------------------------- #
# Section 32 of the billing test plan — CU-01 to CU-04
# --------------------------------------------------------------------------- #

class CurrencyTests(PricingTestCase):
    """CU-01 to CU-04 — INR only, deliberately.

    ``Plan.CurrencyChoices`` has a single member, every amount is a ``Decimal`` of
    rupees, and the conversion to paise lives in one place. Adding a second currency
    later means per-currency plans, a currency on the wallet or a conversion at the
    boundary, and a decision about what a credit is worth in each. None of that is
    designed, and writing tests as though it were would be misleading.
    """

    def test_cu01_inr_is_the_only_currency_a_plan_can_have(self):
        """CU-01: a single-member enum is the cheapest possible guard against a
        second currency arriving without any of the design that would need."""
        self.assertEqual(list(Plan.CurrencyChoices.values), ["INR"])
        self.assertEqual(self.starter.currency, "INR")

    def test_cu02_gateway_amounts_are_rupees_multiplied_into_paise(self):
        """CU-02: **₹708.00 is 70800 paise.**

        Sending 708 would charge the tenant seven rupees and eight paise, and the
        payment would succeed — which is why this is asserted rather than assumed.
        """
        quote = pricing.quote_top_up(self.subscription, 500)

        self.assertEqual(quote.total, Decimal("708.00"))
        self.assertEqual(int(quote.total * 100), 70800)

    def test_cu03_fractions_of_a_paisa_are_resolved_before_conversion(self):
        """CU-03: an unquantised Decimal would convert to a non-integer number of
        paise, and the gateway takes an integer."""
        quote = pricing.quote_addon(
            self.subscription, Resource.STORAGE_GB, 20, now=self.now
        )

        self.assertEqual(quote.total.as_tuple().exponent, -2)
        self.assertEqual(int(quote.total * 100), int(quote.total * 100))
        self.assertEqual(quote.total * 100 % 1, 0)

    def test_cu04_an_order_response_names_its_currency(self):
        """CU-04: the checkout widget reads it, and a missing field would make it
        guess."""
        from onboard.tests.billing_factories import BILLING

        response = self.ctx.client.post(
            f"{BILLING}/top-up/", {"credits": 500}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["currency"], "INR")
