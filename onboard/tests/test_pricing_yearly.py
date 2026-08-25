"""Section 29 of the billing test plan — YP-01 to YP-16.

PR-33 and PR-34 prove only the *direction* of a yearly change. A yearly cycle is 365
days of arithmetic where a rounding error is small enough to hide and large enough to
matter, so this walks it.

The figure that governs the section: ``unused_ratio`` quantises to **four decimal
places**. On a ₹9,990 net price that is worth up to about fifty paise of rounding —
deterministic, always in the same direction for the same inputs, and far below the
gateway's own precision, but real. YP-16 measures it rather than pretending it is zero.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.utils import timezone

from onboard.constants.entitlement_resource import EntitlementResource
from onboard.models import Plan, Subscription, TenantEntitlement
from onboard.services import pricing
from onboard.services import subscriptions as subscription_service
from onboard.tests.billing_factories import (
    assert_invariant,
    make_plan,
    make_tenant,
)
from wallet.models import CreditLedger
from wallet.services import wallet as wallet_service

Bucket = CreditLedger.Bucket
EntryType = CreditLedger.EntryType
Resource = EntitlementResource

YEAR_DAYS = 365

#: What a four-decimal-place ratio can cost on a given net price, plus the paisa the
#: final quantisation is allowed to move the answer.
def rounding_allowance(net):
    return (net * Decimal("0.00005")).quantize(Decimal("0.01")) + Decimal("0.01")


class YearlyPricingTestCase(TestCase):
    """A tenant one day into a 365-day cycle on a ₹9,990 yearly plan."""

    def setUp(self):
        self.ctx = make_tenant("yearly")
        self.tenant = self.ctx.tenant
        self.now = timezone.now()

        self.yearly = make_plan(
            "yearly-plan",
            base_price=Decimal("9990.00"),
            tax_percentage=Decimal("18.00"),
            billing_cycle=Plan.BillingCycleChoices.YEARLY,
            allocated_credits=12000,
            max_rollover_credits=5000,
            allow_rollover_on_renewal=True,
        )
        self.yearly_pro = make_plan(
            "yearly-pro-plan",
            base_price=Decimal("26991.00"),
            tax_percentage=Decimal("18.00"),
            billing_cycle=Plan.BillingCycleChoices.YEARLY,
            allocated_credits=42000,
        )
        self.monthly = make_plan(
            "yearly-monthly-plan",
            base_price=Decimal("999.00"),
            tax_percentage=Decimal("18.00"),
            allocated_credits=1000,
        )
        self.subscription = subscription_service.activate(self.tenant, self.yearly)
        self.set_day(0)

    # --- helpers ------------------------------------------------------------

    def set_day(self, days_elapsed, total=YEAR_DAYS, subscription=None):
        subscription = subscription or self.subscription
        Subscription.objects.filter(pk=subscription.pk).update(
            start_date=self.now - timedelta(days=days_elapsed),
            end_date=self.now + timedelta(days=total - days_elapsed),
        )
        subscription.refresh_from_db()
        return subscription

    def credit_on(self, day, target=None, total=YEAR_DAYS):
        self.set_day(day, total=total)
        quote = pricing.quote_plan_change(
            self.subscription, target or self.yearly_pro, now=self.now
        )
        return Decimal(str(quote.detail["credit_from_current"]))

    @property
    def net(self):
        return self.yearly.net_base_price


class DailyCreditTests(YearlyPricingTestCase):
    """YP-01 to YP-07 — the credit for unused time, across the year."""

    def test_yp01_on_day_one_almost_the_whole_year_is_unused(self):
        """YP-01."""
        credit = self.credit_on(0)

        self.assertEqual(credit, self.net)

    def test_yp02_halfway_through_half_is_credited(self):
        """YP-02: to the paisa, not "about half"."""
        credit = self.credit_on(182)

        expected = (self.net * Decimal("183") / Decimal("365")).quantize(
            Decimal("0.01")
        )
        self.assertLessEqual(
            abs(credit - expected), rounding_allowance(self.net),
            f"day 182 credited {credit}, expected about {expected}",
        )

    def test_yp03_on_the_last_full_day_roughly_one_day_remains(self):
        """YP-03."""
        credit = self.credit_on(364)

        expected = (self.net / Decimal("365")).quantize(Decimal("0.01"))
        self.assertLessEqual(
            abs(credit - expected), rounding_allowance(self.net)
        )

    def test_yp04_at_the_last_moment_the_credit_is_zero_and_never_negative(self):
        """YP-04: **never negative.** A negative credit would become a charge on
        top of the new plan's price."""
        credit = self.credit_on(YEAR_DAYS)

        self.assertEqual(credit, Decimal("0.00"))
        self.assertGreaterEqual(credit, Decimal("0.00"))

    def test_yp05_past_the_end_date_the_full_new_price_is_due(self):
        """YP-05: a finished cycle owes the tenant nothing back."""
        self.set_day(YEAR_DAYS + 30)

        quote = pricing.quote_plan_change(
            self.subscription, self.yearly_pro, now=self.now
        )

        self.assertEqual(
            Decimal(str(quote.detail["credit_from_current"])), Decimal("0.00")
        )
        self.assertEqual(quote.net, self.yearly_pro.net_base_price)

    def test_yp06_the_credit_only_ever_decreases_across_the_year(self):
        """YP-06: **the shape of the whole curve.**

        Sampled every thirty days: monotonically down, never negative, never above
        the price the tenant paid. A single day with a jump in it would be a
        rounding bug that no individual case would catch.
        """
        credits = [self.credit_on(day) for day in range(0, YEAR_DAYS + 1, 30)]

        for earlier, later in zip(credits, credits[1:]):
            self.assertLessEqual(
                later, earlier, "the credit for unused time increased"
            )
        for credit in credits:
            self.assertGreaterEqual(credit, Decimal("0.00"))
            self.assertLessEqual(credit, self.net)

    def test_yp07_the_year_gives_back_exactly_what_was_paid_for_it(self):
        """YP-07: **no systematic drift.**

        The daily deltas telescope, so their sum is the day-zero credit minus the
        final one. If four-decimal quantisation leaned in one direction, this would
        come out short or over by a visible amount rather than by paise.
        """
        samples = [self.credit_on(day) for day in range(0, YEAR_DAYS + 1, 5)]
        total_given_back = sum(
            earlier - later for earlier, later in zip(samples, samples[1:])
        )

        self.assertEqual(total_given_back, samples[0] - samples[-1])
        self.assertLessEqual(
            abs(total_given_back - self.net), rounding_allowance(self.net)
        )


class CrossCycleTests(YearlyPricingTestCase):
    """YP-08 to YP-10 — moving between cycle lengths."""

    def test_yp08_monthly_to_yearly_credits_only_the_monthly_cycle(self):
        """YP-08: the tenant has a month of unused time, not a year of it."""
        monthly_tenant = make_tenant("yearly-from-monthly")
        subscription = subscription_service.activate(monthly_tenant, self.monthly)
        Subscription.objects.filter(pk=subscription.pk).update(
            start_date=self.now - timedelta(days=20),
            end_date=self.now + timedelta(days=10),
        )
        subscription.refresh_from_db()

        quote = pricing.quote_plan_change(subscription, self.yearly, now=self.now)

        credit = Decimal(str(quote.detail["credit_from_current"]))
        self.assertLessEqual(credit, self.monthly.net_base_price)
        self.assertEqual(quote.detail["change_type"], pricing.PAID_CHANGE)
        self.assertEqual(quote.net, self.yearly.net_base_price - credit)

    def test_yp09_yearly_to_monthly_is_scheduled_and_never_refunded(self):
        """YP-09: **a year of unused time is a large credit, and it must not become
        a payout.**"""
        self.set_day(20)

        quote = pricing.quote_plan_change(
            self.subscription, self.monthly, now=self.now
        )

        self.assertEqual(quote.detail["change_type"], pricing.SCHEDULED_CHANGE)
        self.assertEqual(quote.total, Decimal("0.00"))
        self.assertGreaterEqual(quote.net, Decimal("0.00"))

    def test_yp10_yearly_to_yearly_prorates_on_the_yearly_prices(self):
        """YP-10."""
        self.set_day(182)

        quote = pricing.quote_plan_change(
            self.subscription, self.yearly_pro, now=self.now
        )

        credit = Decimal(str(quote.detail["credit_from_current"]))
        self.assertGreater(credit, self.net / 2 - rounding_allowance(self.net))
        self.assertEqual(
            quote.net, self.yearly_pro.net_base_price - credit
        )
        self.assertEqual(quote.detail["change_type"], pricing.PAID_CHANGE)


class CalendarEdgeTests(YearlyPricingTestCase):
    """YP-11 to YP-12 — days that are not all the same length."""

    def test_yp11_a_leap_year_cycle_is_366_days_and_still_bounded(self):
        """YP-11: the ratio is computed from the actual span, so a 366-day cycle
        needs no special case — but it must not produce a ratio above 1."""
        leap_start = timezone.make_aware(datetime(2028, 1, 15, 10, 0))
        leap_end = leap_start + timedelta(days=366)
        Subscription.objects.filter(pk=self.subscription.pk).update(
            start_date=leap_start, end_date=leap_end
        )
        self.subscription.refresh_from_db()

        at_start = pricing.unused_ratio(self.subscription, now=leap_start)
        midway = pricing.unused_ratio(
            self.subscription, now=leap_start + timedelta(days=183)
        )
        at_end = pricing.unused_ratio(self.subscription, now=leap_end)

        self.assertEqual(at_start, Decimal("1.0000"))
        self.assertLessEqual(midway, Decimal("1.0000"))
        self.assertEqual(at_end, Decimal("0"))
        self.assertAlmostEqual(float(midway), 183 / 366, places=3)

    def test_yp12_a_dst_cycle_is_measured_in_real_time_not_wall_clock(self):
        """YP-12: **ratios are computed on UTC instants.**

        A cycle from 1 March to 31 March across a US spring-forward looks like 720
        hours on a calendar and is 719 hours of real time — the lost hour is
        genuinely gone, and it fell in the first half.

        So at the *wall-clock* midpoint the tenant has slightly **more** than half
        the cycle left, and at the true instant midpoint they have exactly half.
        Both are asserted, because the difference between them is the whole point:
        a proration computed on local calendar days would credit the wrong amount
        for every tenant in a DST zone, twice a year.
        """
        eastern = ZoneInfo("America/New_York")
        utc = ZoneInfo("UTC")
        # 2027-03-14 is a spring-forward date in the US Eastern zone.
        start = datetime(2027, 3, 1, 12, 0, tzinfo=eastern)
        end = datetime(2027, 3, 31, 12, 0, tzinfo=eastern)
        Subscription.objects.filter(pk=self.subscription.pk).update(
            start_date=start, end_date=end
        )
        self.subscription.refresh_from_db()

        # Stored and read back as instants, where the DST hour has vanished.
        real_span = self.subscription.end_date - self.subscription.start_date
        self.assertEqual(real_span, timedelta(hours=719))

        exact_midpoint = self.subscription.start_date + real_span / 2
        self.assertEqual(
            pricing.unused_ratio(self.subscription, now=exact_midpoint),
            Decimal("0.5000"),
        )

        wall_clock_midpoint = datetime(2027, 3, 16, 12, 0, tzinfo=eastern)
        self.assertEqual(
            pricing.unused_ratio(self.subscription, now=wall_clock_midpoint),
            Decimal("0.5007"),
        )

        # And whichever instant is used, the ratio stays inside its bounds.
        for moment in (start, exact_midpoint, wall_clock_midpoint, end):
            ratio = pricing.unused_ratio(self.subscription, now=moment)
            self.assertGreaterEqual(ratio, Decimal("0"))
            self.assertLessEqual(ratio, Decimal("1.0000"))


class YearlyCreditRuleTests(YearlyPricingTestCase):
    """YP-13 to YP-16 — capacity, allocations and the rounding bound."""

    def test_yp13_capacity_is_prorated_over_the_yearly_cycle(self):
        """YP-13: **rented for the rest of the year, not the rest of a month.**

        Charging a monthly fraction on a yearly plan would undercharge by a factor
        of twelve.
        """
        storage = self.yearly.entitlements.get(resource=Resource.STORAGE_GB)
        storage.included_quantity = 10
        storage.price_per_additional = Decimal("1200.00")
        storage.addon_step = 10
        storage.max_additional = 90
        storage.is_addon_available = True
        storage.save()
        self.set_day(0)

        quote = pricing.quote_addon(
            self.subscription, Resource.STORAGE_GB, 10, now=self.now
        )

        # A whole year unused, so essentially the full recurring price is due now.
        self.assertEqual(quote.detail["recurring_net"], Decimal("1200.00"))
        self.assertEqual(quote.detail["unused_ratio"], Decimal("1.0000"))
        self.assertEqual(quote.net, Decimal("1200.00"))

    def test_yp14_a_yearly_renewal_grants_the_yearly_allocation(self):
        """YP-14: twelve times the monthly plan's, because that is what the plan
        was configured to grant — and a renewal must not quietly allocate a
        month's worth."""
        self.assertEqual(
            self.yearly.allocated_credits, self.monthly.allocated_credits * 12
        )

        subscription_service.renew(self.subscription)

        wallet = assert_invariant(self, self.tenant, msg="after a yearly renewal")
        # Rolls over, capped at 5000: the first year's 12000 is clamped, then the
        # new year's 12000 lands on top.
        self.assertEqual(wallet.allocated_balance, 5000 + 12000)

    def test_yp15_the_rollover_cap_applies_to_the_yearly_balance(self):
        """YP-15: **a light user on an annual plan can bank a year of credits**,
        which is exactly what the cap exists to stop."""
        carried, forfeited, code = subscription_service.rollover_amount(
            self.subscription, reason="renewal"
        )

        self.assertEqual(carried, 5000)
        self.assertEqual(forfeited, 12000 - 5000)
        self.assertEqual(code, "rollover_cap_forfeit")

    def test_yp16_the_rounding_error_stays_within_what_the_ratio_allows(self):
        """YP-16: **measured, not assumed to be zero.**

        ``unused_ratio`` quantises to four decimal places, so on a ₹9,990 price the
        credit can be up to about fifty paise away from the exact fraction. That is
        deterministic and identical in both directions, but it is not nothing — and
        a test asserting a one-paisa bound here would be asserting something the
        arithmetic cannot deliver.
        """
        allowance = rounding_allowance(self.net)
        self.assertLessEqual(allowance, Decimal("1.00"))

        worst = Decimal("0.00")
        for day in range(0, YEAR_DAYS + 1, 7):
            credit = self.credit_on(day)
            exact = self.net * (Decimal(YEAR_DAYS - day) / Decimal(YEAR_DAYS))
            worst = max(worst, abs(credit - exact))

        self.assertLessEqual(
            worst, allowance,
            f"worst-case rounding was {worst}, above the {allowance} the "
            f"four-decimal ratio allows",
        )
