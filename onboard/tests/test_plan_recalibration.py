"""Phase 9.1 — thresholds and caps expressed against the cycle's own grant.

Both settings were absolute credit counts, which worked while every subscriber on a
plan got the same allocation. Across a band the grant varies several-fold, and one
fixed number cannot serve both ends of it.

The rollover cap is the one that matters most: it does not merely mis-time a warning,
it **destroys credits a customer paid for**, quietly, at every renewal.

Run with ``--settings=config.settings.test_postgres``.
"""

from decimal import Decimal

from django.test import TestCase

from onboard.services import subscriptions as subscription_service
from onboard.tests.billing_factories import (
    DEFAULT_RATES,
    Operation,
    make_minutes_plan,
    make_plan,
    make_tenant,
    subscribe,
)
from wallet.services import wallet as wallet_service

HIGH = Operation.CONVERSATION_HIGH_LATENCY


class LowBalanceResolutionTests(TestCase):

    def test_a_percentage_scales_with_the_grant(self):
        plan = make_plan(low_balance_percent=10, low_balance_threshold=350)

        self.assertEqual(plan.low_balance_for(4_000), 400)
        self.assertEqual(plan.low_balance_for(16_000), 1_600)

    def test_the_absolute_value_is_the_fallback(self):
        plan = make_plan(low_balance_percent=None, low_balance_threshold=350)

        self.assertEqual(plan.low_balance_for(16_000), 350)

    def test_a_percentage_never_rounds_down_to_zero(self):
        """Rounding to zero silently disables the alert for exactly the customers
        with the least room to spare."""
        plan = make_plan(low_balance_percent=10, low_balance_threshold=0)

        self.assertEqual(plan.low_balance_for(5), 1)

    def test_no_grant_falls_back_rather_than_computing_zero(self):
        plan = make_plan(low_balance_percent=10, low_balance_threshold=350)

        self.assertEqual(plan.low_balance_for(0), 350)
        self.assertEqual(plan.low_balance_for(None), 350)


class RolloverCapResolutionTests(TestCase):

    def test_a_ratio_scales_with_the_grant(self):
        plan = make_plan(max_rollover_ratio=Decimal("1.00"), max_rollover_credits=3500)

        self.assertEqual(plan.rollover_cap_for(4_000), 4_000)
        self.assertEqual(plan.rollover_cap_for(16_000), 16_000)

    def test_a_ratio_rounds_down(self):
        """A cap is a limit; rounding a limit up grants more than the plan says."""
        plan = make_plan(max_rollover_ratio=Decimal("0.50"))

        self.assertEqual(plan.rollover_cap_for(4_001), 2_000)

    def test_the_absolute_cap_is_the_fallback(self):
        plan = make_plan(max_rollover_ratio=None, max_rollover_credits=3500)

        self.assertEqual(plan.rollover_cap_for(16_000), 3_500)

    def test_uncapped_stays_uncapped(self):
        plan = make_plan(max_rollover_ratio=None, max_rollover_credits=None)

        self.assertIsNone(plan.rollover_cap_for(16_000))


class RolloverAppliesTheRatioTests(TestCase):
    """The behaviour the ratio exists to fix, end to end."""

    def _subscription(self, minutes, **plan_kwargs):
        plan_kwargs.setdefault("min_minutes", None)
        plan_kwargs.setdefault("max_minutes", None)
        plan_kwargs.setdefault("credit_basis", HIGH)
        plan_kwargs.setdefault("allow_rollover_on_renewal", True)
        plan = make_minutes_plan(**plan_kwargs)
        return subscribe(make_tenant().tenant, plan, minutes=minutes), plan

    def test_a_fixed_cap_punishes_the_larger_customer(self):
        """The status quo, asserted so the fix has something to be measured against.

        Both hold exactly one cycle's grant and neither has overspent. A cap of 3,500
        forfeits nothing from the 400-minute customer (3,200 credits) and burns 12,500
        from the 2,000-minute one — for the identical behaviour.
        """
        small, _ = self._subscription(400, max_rollover_credits=3500)
        large, _ = self._subscription(2000, max_rollover_credits=3500)

        _, small_forfeited, _ = subscription_service.rollover_amount(
            small, reason="renewal"
        )
        _, large_forfeited, _ = subscription_service.rollover_amount(
            large, reason="renewal"
        )

        self.assertEqual(small_forfeited, 0)
        self.assertGreater(large_forfeited, 0)

    def test_a_ratio_treats_both_customers_the_same(self):
        small, _ = self._subscription(
            400, max_rollover_credits=3500, max_rollover_ratio=Decimal("1.00")
        )
        large, _ = self._subscription(
            2000, max_rollover_credits=3500, max_rollover_ratio=Decimal("1.00")
        )

        for subscription in (small, large):
            carried, forfeited, code = subscription_service.rollover_amount(
                subscription, reason="renewal"
            )
            with self.subTest(minutes=subscription.selected_minutes):
                self.assertEqual(forfeited, 0)
                self.assertEqual(carried, subscription.allocated_credits)
                self.assertIsNone(code)

    def test_a_ratio_below_one_still_forfeits_proportionally(self):
        subscription, _ = self._subscription(
            1000, max_rollover_ratio=Decimal("0.50")
        )
        granted = subscription.allocated_credits

        carried, forfeited, code = subscription_service.rollover_amount(
            subscription, reason="renewal"
        )

        self.assertEqual(carried, granted // 2)
        self.assertEqual(forfeited, granted - granted // 2)
        self.assertEqual(code, "rollover_cap_forfeit")

    def test_the_cap_comes_from_the_frozen_terms(self):
        """A cap tightened today must not retroactively burn credits granted under
        the old terms."""
        subscription, plan = self._subscription(
            1000, max_rollover_ratio=Decimal("1.00")
        )
        plan.max_rollover_ratio = Decimal("0.10")
        plan.save(update_fields=["max_rollover_ratio"])

        _, forfeited, _ = subscription_service.rollover_amount(
            subscription, reason="renewal"
        )

        self.assertEqual(forfeited, 0)

    def test_an_unreadable_ratio_falls_back_instead_of_raising(self):
        subscription, _ = self._subscription(1000, max_rollover_credits=100)
        snapshot = dict(subscription.plan_snapshot)
        snapshot["max_rollover_ratio"] = "not-a-number"
        subscription.plan_snapshot = snapshot
        subscription.save(update_fields=["plan_snapshot"])

        with self.assertLogs("onboard.services.subscriptions", level="WARNING"):
            carried, _, _ = subscription_service.rollover_amount(
                subscription, reason="renewal"
            )

        self.assertEqual(carried, 100)


class WalletThresholdTests(TestCase):
    """The alert level reaches the wallet, where the low-balance check reads it."""

    def test_activation_stamps_the_computed_threshold(self):
        plan = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
            low_balance_percent=10, low_balance_threshold=350,
        )
        ctx = make_tenant()

        subscription = subscribe(ctx.tenant, plan, minutes=1000)

        wallet = wallet_service.for_tenant(ctx.tenant)
        self.assertEqual(
            wallet.low_balance_threshold, subscription.allocated_credits // 10
        )

    def test_two_quantities_on_one_plan_get_different_thresholds(self):
        plan = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
            low_balance_percent=10,
        )
        small = make_tenant()
        large = make_tenant()

        subscribe(small.tenant, plan, minutes=500)
        subscribe(large.tenant, plan, minutes=2000)

        self.assertEqual(
            wallet_service.for_tenant(small.tenant).low_balance_threshold,
            500 * DEFAULT_RATES[HIGH] // 10,
        )
        self.assertEqual(
            wallet_service.for_tenant(large.tenant).low_balance_threshold,
            2000 * DEFAULT_RATES[HIGH] // 10,
        )

    def test_a_legacy_plan_still_stamps_its_absolute_threshold(self):
        plan = make_plan(low_balance_percent=None, low_balance_threshold=350)
        ctx = make_tenant()

        subscribe(ctx.tenant, plan)

        self.assertEqual(
            wallet_service.for_tenant(ctx.tenant).low_balance_threshold, 350
        )
