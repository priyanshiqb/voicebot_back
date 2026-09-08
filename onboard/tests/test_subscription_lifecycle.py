"""Section 9 of the billing test plan — SL-01 to SL-55.

``onboard.services.subscriptions`` is the one place a subscription comes into
existence. Three signup paths used to do it inline, and every one of them produced a
bare row with no end date, no credits and no rate card.

**Order is load-bearing throughout.** Stamping before granting, forfeiting before
allocating: get either backwards and the tenant is charged at the wrong rate or loses
credits they just paid for. Both orderings are asserted directly rather than inferred
from a final balance that happens to look right.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from onboard.models import Plan, Subscription
from onboard.services import pricing
from onboard.services import subscriptions as subscription_service
from onboard.tests.billing_factories import (
    assert_invariant,
    make_free_plan,
    add_rates,
    make_plan,
    make_tenant,
    make_transaction,
)
from wallet.models import CreditLedger, CreditRate, Wallet
from wallet.services import wallet as wallet_service

Bucket = CreditLedger.Bucket
EntryType = CreditLedger.EntryType
Operation = CreditRate.Operation
Status = Subscription.SubscriptionStatusChoices


class LifecycleTestCase(TestCase):
    """A tenant and two priced plans to move between."""

    def setUp(self):
        self.ctx = make_tenant("lifecycle")
        self.tenant = self.ctx.tenant
        self.plan = make_plan(
            "lifecycle-plan",
            allocated_credits=1000,
            low_balance_threshold=100,
            rates={Operation.CONVERSATION_HIGH_LATENCY: 5},
        )
        self.bigger = make_plan(
            "lifecycle-bigger",
            base_price=Decimal("3000.00"),
            allocated_credits=3000,
            low_balance_threshold=300,
            rates={Operation.CONVERSATION_HIGH_LATENCY: 3},
        )

    @property
    def wallet(self):
        return Wallet.objects.get(tenant=self.tenant)

    def rows(self, **filters):
        return CreditLedger.all_objects.filter(tenant=self.tenant, **filters)

    def top_up(self, amount, reference="lifecycle-top-up"):
        return wallet_service.grant(
            self.tenant, entry_type=EntryType.MANUAL_TOP_UP,
            amount=amount, bucket=Bucket.PURCHASED, reference_id=reference,
        )


class ActivateTests(LifecycleTestCase):
    """SL-01 to SL-13."""

    def test_sl01_a_fresh_tenant_gets_a_live_subscription(self):
        """SL-01."""
        subscription = subscription_service.activate(self.tenant, self.plan)

        self.assertEqual(subscription.status, Status.ACTIVE)
        self.assertIsNotNone(subscription.end_date)
        self.assertGreater(subscription.end_date, timezone.now())

    def test_sl02_the_wallet_is_stamped_with_the_plans_rate_card(self):
        """SL-02: without this the tenant's first call raises
        ``RateNotConfigured`` — which is exactly what every pre-existing tenant on
        this platform was doing until the stamps were backfilled."""
        subscription_service.activate(self.tenant, self.plan)

        snapshot = self.wallet.rate_snapshot
        self.assertEqual(snapshot["plan_code"], self.plan.code)
        self.assertEqual(len(snapshot["operations"]), 3)

    def test_sl03_the_cycles_credits_are_granted(self):
        """SL-03."""
        subscription_service.activate(self.tenant, self.plan)

        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.balance, 1000)
        self.assertEqual(wallet.allocated_balance, 1000)

    def test_sl04_the_alert_threshold_comes_from_the_plan(self):
        """SL-04."""
        subscription_service.activate(self.tenant, self.plan)

        self.assertEqual(self.wallet.low_balance_threshold, 100)

    def test_sl05_the_grant_is_keyed_on_the_payment_order(self):
        """SL-05: **this is what makes a redelivered webhook harmless.**

        Razorpay delivering ``payment.captured`` twice must not grant the cycle's
        credits twice, and the order id is the only key both deliveries share.
        """
        order = make_transaction(self.tenant, self.plan)

        subscription_service.activate(self.tenant, self.plan, transaction=order)

        grant = self.rows(entry_type=EntryType.CYCLE_ALLOCATION).get()
        self.assertEqual(grant.reference_id, order.razorpay_order_id)

    def test_sl06_the_ledger_row_names_the_cycle_it_belongs_to(self):
        """SL-06: audit context. Nothing reads it to make a decision, which is what
        keeps the wallet independent of the subscription engine."""
        subscription = subscription_service.activate(self.tenant, self.plan)

        grant = self.rows(entry_type=EntryType.CYCLE_ALLOCATION).get()
        self.assertEqual(grant.subscription_id, subscription.pk)

    def test_sl07_the_rate_card_is_stamped_before_any_credits_land(self):
        """SL-07: **the ordering, asserted directly.**

        A grant that lands first is credited while the wallet still carries the
        previous plan's prices, and a call arriving between the two is rated at the
        wrong tier. A final-balance assertion cannot see that.
        """
        observed = {}
        real_grant = wallet_service.grant

        def spy(tenant, **kwargs):
            wallet = Wallet.objects.get(tenant_id=getattr(tenant, "id", tenant))
            observed["stamped_before_grant"] = bool(wallet.rate_snapshot)
            observed["plan_code_at_grant"] = (wallet.rate_snapshot or {}).get("plan_code")
            return real_grant(tenant, **kwargs)

        with patch.object(wallet_service, "grant", side_effect=spy):
            subscription_service.activate(self.tenant, self.plan)

        self.assertTrue(observed["stamped_before_grant"])
        self.assertEqual(observed["plan_code_at_grant"], self.plan.code)

    def test_sl08_activating_twice_for_the_same_plan_grants_credits_once(self):
        """SL-08: **the browser-confirmed payment and the webhook both complete
        onboarding, and both can win.** Whichever arrives second must be a no-op."""
        first = subscription_service.activate(self.tenant, self.plan)
        second = subscription_service.activate(self.tenant, self.plan)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(self.rows(entry_type=EntryType.CYCLE_ALLOCATION).count(), 1)
        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.balance, 1000)

    def test_sl09_without_a_transaction_the_subscription_id_is_the_key(self):
        """SL-09: a free-plan signup has no order to key on, and must still be
        idempotent."""
        subscription = subscription_service.activate(self.tenant, self.plan)

        grant = self.rows(entry_type=EntryType.CYCLE_ALLOCATION).get()
        self.assertEqual(grant.reference_id, f"subscription:{subscription.pk}")

        subscription_service.activate(self.tenant, self.plan)
        self.assertEqual(self.rows(entry_type=EntryType.CYCLE_ALLOCATION).count(), 1)

    def test_sl10_activating_a_different_plan_closes_the_old_subscription(self):
        """SL-10: closed *before* the new row is inserted — the unique constraint
        is checked per statement, so insert-then-close fails."""
        old = subscription_service.activate(self.tenant, self.plan)

        new = subscription_service.activate(self.tenant, self.bigger)

        old.refresh_from_db()
        self.assertEqual(old.status, Status.CANCELLED)
        self.assertIsNotNone(old.canceled_at)
        self.assertEqual(old.superseded_by_id, new.pk)
        self.assertEqual(
            Subscription.objects.filter(
                tenant=self.tenant, status=Status.ACTIVE
            ).count(),
            1,
        )

    def test_sl11_a_plan_that_grants_nothing_writes_no_row(self):
        """SL-11: a zero-amount ledger row is rejected by the non-zero constraint,
        and would be noise even if it were not."""
        # A plan cannot describe "grants nothing" any more -- the grant is minutes
        # times credits-per-minute and both are positive. `credit_grant=0` is where
        # that decision lives now, and the assertion below is unchanged.
        no_credits = make_plan("lifecycle-no-credits")

        subscription_service.activate(self.tenant, no_credits, credit_grant=0)

        self.assertEqual(self.rows(entry_type=EntryType.CYCLE_ALLOCATION).count(), 0)
        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.balance, 0)

    def test_sl12_a_wallet_failure_does_not_undo_the_subscription(self):
        """SL-12: **the tenant has paid, and the subscription is real.**

        Leaving them with an unfunded wallet is a support ticket; refusing the whole
        activation is a lost customer. The reconciliation and unrated-call jobs are
        what surface the gap.
        """
        with patch.object(
            wallet_service, "set_plan_context", side_effect=RuntimeError("db down")
        ):
            with self.assertLogs("onboard.services.subscriptions", level="ERROR"):
                subscription = subscription_service.activate(self.tenant, self.plan)

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Status.ACTIVE)
        self.assertEqual(self.rows(entry_type=EntryType.CYCLE_ALLOCATION).count(), 0)

    def test_sl13_a_free_plan_is_activated_the_same_way(self):
        """SL-13: trial credits and a full rate card priced at zero. A free plan
        with *missing* rates would raise on the tenant's first call."""
        free = make_free_plan("lifecycle-free")

        subscription_service.activate(self.tenant, free)

        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.balance, free.credits_for(free.min_minutes))
        self.assertEqual(len(wallet.rate_snapshot["operations"]), 3)


class RolloverAmountTests(LifecycleTestCase):
    """SL-14 to SL-20 — how much of a balance survives a transition."""

    def subscribe_with(self, **plan_overrides):
        plan = make_plan(f"rollover-{len(plan_overrides)}-plan", **plan_overrides)
        return subscription_service.activate(self.tenant, plan), plan

    def test_sl14_a_plan_that_does_not_roll_over_forfeits_everything(self):
        """SL-14."""
        subscription, _ = self.subscribe_with(
            allocated_credits=1000, allow_rollover_on_renewal=False
        )

        carried, forfeited, code = subscription_service.rollover_amount(
            subscription, reason="renewal"
        )

        self.assertEqual((carried, forfeited), (0, 1000))
        self.assertEqual(code, "rollover_forfeit")

    def test_sl15_under_the_cap_everything_carries(self):
        """SL-15."""
        subscription, _ = self.subscribe_with(
            allocated_credits=1000,
            allow_rollover_on_renewal=True,
            max_rollover_credits=5000,
        )

        self.assertEqual(
            subscription_service.rollover_amount(subscription, reason="renewal"),
            (1000, 0, None),
        )

    def test_sl16_over_the_cap_the_excess_is_forfeited(self):
        """SL-16: and under its own reason code — 'you hit the cap' is a different
        conversation from 'your plan does not roll over'."""
        subscription, _ = self.subscribe_with(
            allocated_credits=1000,
            allow_rollover_on_renewal=True,
            max_rollover_credits=400,
        )

        carried, forfeited, code = subscription_service.rollover_amount(
            subscription, reason="renewal"
        )

        self.assertEqual((carried, forfeited), (400, 600))
        self.assertEqual(code, "rollover_cap_forfeit")

    def test_sl17_a_null_cap_means_no_cap(self):
        """SL-17."""
        subscription, _ = self.subscribe_with(
            allocated_credits=1000,
            allow_rollover_on_renewal=True,
            max_rollover_credits=None,
        )

        self.assertEqual(
            subscription_service.rollover_amount(subscription, reason="renewal"),
            (1000, 0, None),
        )

    def test_sl18_the_outgoing_subscriptions_frozen_rules_decide(self):
        """SL-18: **the terms that govern a balance are the ones the tenant agreed
        to when they were granted it** — not the ones an admin set this morning."""
        subscription, plan = self.subscribe_with(
            allocated_credits=1000,
            allow_rollover_on_renewal=True,
            max_rollover_credits=None,
        )

        plan.allow_rollover_on_renewal = False
        plan.max_rollover_credits = 0
        plan.save(update_fields=["allow_rollover_on_renewal", "max_rollover_credits"])

        self.assertEqual(
            subscription_service.rollover_amount(subscription, reason="renewal"),
            (1000, 0, None),
        )

    def test_sl19_purchased_credits_are_never_at_stake(self):
        """SL-19: **they were bought with cash.** Expiring them would be taking
        money for nothing, whatever the plan's rollover flags say."""
        subscription, _ = self.subscribe_with(
            allocated_credits=1000, allow_rollover_on_renewal=False
        )
        self.top_up(750)

        carried, forfeited, _ = subscription_service.rollover_amount(
            subscription, reason="renewal"
        )

        self.assertEqual(carried, 0)
        self.assertEqual(forfeited, 1000, "only the allocated bucket may be forfeited")

    def test_sl20_each_transition_reads_its_own_flag(self):
        """SL-20: a plan can roll over on renewal and not on a downgrade, and the
        three flags must not be conflated."""
        subscription, _ = self.subscribe_with(
            allocated_credits=1000,
            allow_rollover_on_renewal=True,
            allow_rollover_on_upgrade=False,
            allow_rollover_on_downgrade=True,
            max_rollover_credits=None,
        )

        self.assertEqual(
            subscription_service.rollover_amount(subscription, reason="renewal")[0],
            1000,
        )
        self.assertEqual(
            subscription_service.rollover_amount(subscription, reason="upgrade")[0], 0
        )
        self.assertEqual(
            subscription_service.rollover_amount(subscription, reason="downgrade")[0],
            1000,
        )


class RenewTests(LifecycleTestCase):
    """SL-21 to SL-31."""

    def test_sl21_a_renewal_extends_the_same_row(self):
        """SL-21: **a renewal is a continuation.** A new row per cycle would turn a
        long-lived customer's history into a chain of near-identical records."""
        subscription = subscription_service.activate(self.tenant, self.plan)

        renewed = subscription_service.renew(subscription)

        self.assertEqual(renewed.pk, subscription.pk)
        self.assertEqual(Subscription.objects.filter(tenant=self.tenant).count(), 1)

    def test_sl22_the_cycle_moves_one_period_forward(self):
        """SL-22."""
        subscription = subscription_service.activate(self.tenant, self.plan)
        old_end = subscription.end_date

        renewed = subscription_service.renew(subscription)

        self.assertEqual(renewed.start_date, old_end)
        self.assertEqual(renewed.end_date, old_end + renewed.cycle_delta())

    def test_sl23_without_rollover_the_old_allocation_is_burned(self):
        """SL-23."""
        no_rollover = make_plan(
            "renew-no-rollover", allocated_credits=1000,
            allow_rollover_on_renewal=False,
        )
        subscription = subscription_service.activate(self.tenant, no_rollover)

        subscription_service.renew(subscription)

        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.allocated_balance, 1000)
        self.assertTrue(
            self.rows(entry_type=EntryType.ROLLOVER_FORFEIT).exists()
        )

    def test_sl24_with_rollover_the_balances_add_up(self):
        """SL-24."""
        rolling = make_plan(
            "renew-rolling", allocated_credits=1000,
            allow_rollover_on_renewal=True, max_rollover_credits=None,
        )
        subscription = subscription_service.activate(self.tenant, rolling)

        subscription_service.renew(subscription)

        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.allocated_balance, 2000)

    def test_sl25_the_rollover_cap_is_applied_at_renewal(self):
        """SL-25."""
        capped = make_plan(
            "renew-capped", allocated_credits=1000,
            allow_rollover_on_renewal=True, max_rollover_credits=400,
        )
        subscription = subscription_service.activate(self.tenant, capped)

        subscription_service.renew(subscription)

        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.allocated_balance, 1400)
        self.assertTrue(
            self.rows(entry_type=EntryType.ROLLOVER_CAP_FORFEIT).exists()
        )

    def test_sl26_purchased_credits_survive_every_renewal(self):
        """SL-26: **the guarantee that makes top-ups sellable.**"""
        no_rollover = make_plan(
            "renew-keeps-purchased", allocated_credits=1000,
            allow_rollover_on_renewal=False,
        )
        subscription = subscription_service.activate(self.tenant, no_rollover)
        self.top_up(250)

        subscription_service.renew(subscription)
        subscription_service.renew(subscription)

        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.purchased_balance, 250)

    def test_sl27_the_snapshot_is_re_frozen_from_the_live_plan(self):
        """SL-27: **a renewal is a future cycle**, and this is where a price or
        rule change legitimately reaches an existing customer."""
        subscription = subscription_service.activate(self.tenant, self.plan)

        # The price and the grant now live on the basis rate row, and re-pricing means
        # superseding it rather than editing a column. The plan sells 200 minutes, so
        # 7.50/min and 6 credits/min is the same change this test always made:
        # ₹1,500 a cycle, 1,200 credits.
        current = self.plan.basis_rate()
        current.effective_to = timezone.now()
        current.save(update_fields=["effective_to"])
        add_rates(self.plan, {Operation.CONVERSATION_HIGH_LATENCY: (6, Decimal("7.5000"))})

        renewed = subscription_service.renew(subscription)

        self.assertEqual(renewed.plan_snapshot["base_price"], "1500.00")
        self.assertEqual(renewed.rules["allocated_credits"], 1200)

    def test_sl28_the_rate_card_is_re_stamped_at_renewal(self):
        """SL-28."""
        subscription = subscription_service.activate(self.tenant, self.plan)
        rate = CreditRate.objects.get(
            plan=self.plan, operation=Operation.CONVERSATION_HIGH_LATENCY
        )
        rate.credits_per_unit = 7
        rate.save(update_fields=["credits_per_unit"])

        subscription_service.renew(subscription)

        self.assertEqual(
            self.wallet.rate_snapshot["operations"][
                Operation.CONVERSATION_HIGH_LATENCY
            ]["credits_per_unit"],
            7,
        )

    def test_sl29_renewing_a_lapsed_subscription_restores_it(self):
        """SL-29: status back to active **and the wallet unfrozen** — one without
        the other leaves a paying tenant unable to spend."""
        subscription = subscription_service.activate(self.tenant, self.plan)
        Subscription.objects.filter(pk=subscription.pk).update(
            end_date=timezone.now() - timedelta(days=2)
        )
        subscription.refresh_from_db()
        subscription_service.expire(subscription)
        subscription.refresh_from_db()
        self.assertTrue(self.wallet.is_frozen)

        renewed = subscription_service.renew(subscription)

        self.assertEqual(renewed.status, Status.ACTIVE)
        self.assertFalse(self.wallet.is_frozen)

    def test_sl30_a_late_renewal_starts_now_rather_than_back_dating(self):
        """SL-30: back-dating would sell the tenant a cycle they already could not
        use, and expire it early."""
        subscription = subscription_service.activate(self.tenant, self.plan)
        long_ago = timezone.now() - timedelta(days=90)
        Subscription.objects.filter(pk=subscription.pk).update(end_date=long_ago)
        subscription.refresh_from_db()

        renewed = subscription_service.renew(subscription)

        self.assertGreater(renewed.start_date, long_ago)
        self.assertGreater(renewed.end_date, timezone.now())

    def test_sl31_the_forfeit_happens_before_the_new_allocation(self):
        """SL-31: **the ordering, asserted directly.**

        Reversed, the forfeit would eat into the credits the tenant just paid for —
        and the resulting balance on a no-rollover plan would look identical to a
        renewal that never granted anything.
        """
        no_rollover = make_plan(
            "renew-order", allocated_credits=1000,
            allow_rollover_on_renewal=False,
        )
        subscription = subscription_service.activate(self.tenant, no_rollover)

        subscription_service.renew(subscription)

        forfeit = self.rows(entry_type=EntryType.ROLLOVER_FORFEIT).get()
        grants = list(
            self.rows(entry_type=EntryType.CYCLE_ALLOCATION).order_by("created_at")
        )

        self.assertEqual(len(grants), 2)
        self.assertLess(forfeit.created_at, grants[1].created_at)
        self.assertEqual(forfeit.amount, -1000)
        self.assertEqual(grants[1].balance_after, 1000)


class PlanChangeTests(LifecycleTestCase):
    """SL-32 to SL-39."""

    def test_sl32_an_upgrade_closes_the_old_row_and_links_the_chain(self):
        """SL-32: support's first question when a tenant disputes a rollover is
        'what did they have before?', and this is the answer."""
        old = subscription_service.activate(self.tenant, self.plan)

        new = subscription_service.apply_plan_change(old, self.bigger)

        old.refresh_from_db()
        self.assertEqual(old.status, Status.CANCELLED)
        self.assertEqual(old.superseded_by_id, new.pk)
        self.assertEqual(new.status, Status.ACTIVE)
        self.assertEqual(new.plan_id, self.bigger.pk)

    def test_sl33_the_next_call_is_priced_on_the_new_plan(self):
        """SL-33."""
        old = subscription_service.activate(self.tenant, self.plan)

        subscription_service.apply_plan_change(old, self.bigger)

        entry = wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_HIGH_LATENCY,
            quantity=60, reference_id="call-after-change",
        )
        self.assertEqual(abs(entry.amount), 3)

    def test_sl34_earlier_charges_keep_the_rate_they_were_raised_at(self):
        """SL-34: **a plan change re-prices what happens next, not what already
        happened.**"""
        old = subscription_service.activate(self.tenant, self.plan)
        before = wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_HIGH_LATENCY,
            quantity=60, reference_id="call-before-change",
        )
        self.assertEqual(before.rate_applied, 5)

        subscription_service.apply_plan_change(old, self.bigger)

        before.refresh_from_db()
        self.assertEqual(before.rate_applied, 5)
        self.assertEqual(abs(before.amount), 5)

    def test_sl35_the_old_plans_rules_govern_the_rollover(self):
        """SL-35: the balance being carried was granted under the old plan's terms,
        so the new plan has no say over it."""
        strict = make_plan(
            "change-strict", allocated_credits=1000,
            allow_rollover_on_upgrade=False,
        )
        generous = make_plan(
            "change-generous", allocated_credits=2000,
            allow_rollover_on_upgrade=True, max_rollover_credits=None,
        )
        old = subscription_service.activate(self.tenant, strict)

        subscription_service.apply_plan_change(old, generous, reason="upgrade")

        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.allocated_balance, 2000)
        self.assertTrue(self.rows(entry_type=EntryType.ROLLOVER_FORFEIT).exists())

    def test_sl36_scheduling_a_change_leaves_the_current_plan_alone(self):
        """SL-36: **a downgrade is stored, never applied early.** The tenant has
        paid for the cycle they are in."""
        subscription = subscription_service.activate(self.tenant, self.plan)

        scheduled = subscription_service.schedule_plan_change(subscription, self.bigger)

        self.assertEqual(scheduled.plan_id, self.plan.pk)
        self.assertEqual(scheduled.scheduled_plan_id, self.bigger.pk)
        self.assertEqual(scheduled.scheduled_change_at, subscription.end_date)

    def test_sl37_scheduling_creates_no_second_subscription(self):
        """SL-37: storing the intent rather than pre-creating a row is what keeps
        the one-active-per-tenant constraint from ever being approached."""
        subscription = subscription_service.activate(self.tenant, self.plan)

        subscription_service.schedule_plan_change(subscription, self.bigger)

        self.assertEqual(Subscription.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(
            Subscription.objects.filter(
                tenant=self.tenant, status=Status.ACTIVE
            ).count(),
            1,
        )

    def test_sl38_cancelling_a_scheduled_change_clears_both_fields(self):
        """SL-38: a stale ``scheduled_change_at`` with no plan would be a change
        that fires into nothing."""
        subscription = subscription_service.activate(self.tenant, self.plan)
        subscription_service.schedule_plan_change(subscription, self.bigger)

        cleared = subscription_service.cancel_scheduled_change(subscription)

        self.assertIsNone(cleared.scheduled_plan_id)
        self.assertIsNone(cleared.scheduled_change_at)

    def test_sl39_cancelling_nothing_is_harmless(self):
        """SL-39: called implicitly whenever an upgrade is applied, so it has to be
        safe on a subscription that never had anything scheduled."""
        subscription = subscription_service.activate(self.tenant, self.plan)

        result = subscription_service.cancel_scheduled_change(subscription)

        self.assertIsNone(result.scheduled_plan_id)


class BonusDayTests(LifecycleTestCase):
    """An upgrade pays full price and buys extra days with what was left over.

    The money side is settled by `pricing.quote_plan_change`; this is about the
    days actually arriving on the subscription, which is the only place they exist.
    """

    def test_an_upgrade_with_no_bonus_runs_a_standard_cycle(self):
        old = subscription_service.activate(self.tenant, self.plan)

        new = subscription_service.apply_plan_change(old, self.bigger)

        self.assertEqual(new.end_date, new.derive_end_date())

    def test_bonus_days_extend_the_cycle(self):
        old = subscription_service.activate(self.tenant, self.plan)

        new = subscription_service.apply_plan_change(
            old, self.bigger, bonus_days=Decimal("8")
        )

        self.assertEqual(new.end_date - new.derive_end_date(), timedelta(days=8))

    def test_a_fractional_day_is_not_rounded_away(self):
        """A partial day is real money. Dropping it would quietly keep the
        difference on every upgrade."""
        old = subscription_service.activate(self.tenant, self.plan)

        new = subscription_service.apply_plan_change(
            old, self.bigger, bonus_days=Decimal("8.50")
        )

        self.assertEqual(
            new.end_date - new.derive_end_date(), timedelta(days=8, hours=12)
        )

    def test_the_extension_does_not_repeat_at_renewal(self):
        """The bonus is bought once, with time the tenant had already paid for.
        Carrying it into the next cycle would hand it to them again for free."""
        old = subscription_service.activate(self.tenant, self.plan)
        new = subscription_service.apply_plan_change(
            old, self.bigger, bonus_days=Decimal("8")
        )
        extended_end = new.end_date

        renewed = subscription_service.renew(new)

        # One cycle on from the *extended* end, not from a standard one -- and no
        # second helping of the bonus. Compared as dates because cycle_delta() is a
        # relativedelta, which never equals a timedelta even when it spans the same
        # time.
        self.assertEqual(
            renewed.end_date, extended_end + renewed.cycle_delta()
        )

    def test_a_move_to_a_free_plan_buys_no_days(self):
        """Nothing costs anything per day on a free plan, so there is no rate to
        convert the leftover value at. The alternative is an infinite cycle."""
        free = make_free_plan("lifecycle-free")
        old = subscription_service.activate(self.tenant, self.plan)
        old.end_date = timezone.now() + timedelta(days=20)
        old.save(update_fields=["end_date"])

        quote = pricing.quote_plan_change(old, free)

        self.assertEqual(quote.detail["bonus_days"], Decimal("0.00"))

    def test_the_days_come_from_the_quote_the_tenant_paid_on(self):
        """Frozen at order time, not recomputed at apply time. Days pass between
        the two, and recomputing would hand the tenant fewer than they bought."""
        old = subscription_service.activate(self.tenant, self.plan)
        old.end_date = timezone.now() + timedelta(days=20)
        old.save(update_fields=["end_date"])

        quote = pricing.quote_plan_change(old, self.bigger)
        quoted_days = Decimal(str(quote.as_breakdown()["bonus_days"]))

        self.assertGreater(quoted_days, Decimal("0"))
        new = subscription_service.apply_plan_change(
            old, self.bigger, bonus_days=quoted_days
        )
        expected = timedelta(seconds=int(quoted_days * 86400))
        self.assertEqual(new.end_date - new.derive_end_date(), expected)


class ExpireTests(LifecycleTestCase):
    """SL-40 to SL-45."""

    def lapse(self, subscription):
        Subscription.objects.filter(pk=subscription.pk).update(
            end_date=timezone.now() - timedelta(days=1)
        )
        subscription.refresh_from_db()
        return subscription_service.expire(subscription)

    def test_sl40_the_subscription_is_marked_expired(self):
        """SL-40."""
        subscription = subscription_service.activate(self.tenant, self.plan)

        self.assertEqual(self.lapse(subscription).status, Status.EXPIRED)

    def test_sl41_plan_credits_are_forfeited(self):
        """SL-41: that is what 'your plan ended' means."""
        subscription = subscription_service.activate(self.tenant, self.plan)

        self.lapse(subscription)

        self.assertTrue(self.rows(entry_type=EntryType.EXPIRY_FORFEIT).exists())
        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.allocated_balance, 0)

    def test_sl42_purchased_credits_are_frozen_not_confiscated(self):
        """SL-42: **an earlier draft forfeited the whole balance.**

        That would take cash a tenant paid for a top-up and give nothing back.
        Freezing preserves them — unspendable while lapsed, spendable the moment a
        renewal clears.
        """
        subscription = subscription_service.activate(self.tenant, self.plan)
        self.top_up(250)

        self.lapse(subscription)

        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.purchased_balance, 250)
        self.assertEqual(wallet.balance, 250)

    def test_sl43_the_wallet_is_frozen_with_a_reason(self):
        """SL-43."""
        subscription = subscription_service.activate(self.tenant, self.plan)

        self.lapse(subscription)

        wallet = self.wallet
        self.assertTrue(wallet.is_frozen)
        self.assertEqual(wallet.frozen_reason, "subscription_expired")

    def test_sl44_a_plan_that_allows_use_after_expiry_keeps_spending_alive(self):
        """SL-44: **the pay-as-you-go shape.** The subscription lapses, the wallet
        stays live, and neither the freeze nor the forfeit runs."""
        tolerant = make_plan(
            "expire-tolerant", allocated_credits=1000, allow_use_after_expiry=True
        )
        subscription = subscription_service.activate(self.tenant, tolerant)

        self.lapse(subscription)

        wallet = assert_invariant(self, self.tenant)
        self.assertFalse(wallet.is_frozen)
        self.assertEqual(wallet.allocated_balance, 1000)
        self.assertFalse(self.rows(entry_type=EntryType.EXPIRY_FORFEIT).exists())

    def test_sl45_credits_are_spendable_again_after_a_renewal(self):
        """SL-45: the round trip that makes freezing rather than forfeiting the
        right call."""
        subscription = subscription_service.activate(self.tenant, self.plan)
        self.top_up(250)
        self.lapse(subscription)
        subscription.refresh_from_db()

        subscription_service.renew(subscription)

        entry = wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_HIGH_LATENCY,
            quantity=60, reference_id="call-after-renewal",
        )
        self.assertIsNotNone(entry)
        wallet = assert_invariant(self, self.tenant)
        self.assertFalse(wallet.is_frozen)
        self.assertEqual(wallet.purchased_balance, 250)


class CurrentOrExpiredTests(LifecycleTestCase):
    """SL-46 to SL-55 — the resolver reactivation depends on."""

    def lapse(self, subscription):
        Subscription.objects.filter(pk=subscription.pk).update(
            end_date=timezone.now() - timedelta(days=1)
        )
        subscription.refresh_from_db()
        subscription_service.expire(subscription)
        subscription.refresh_from_db()
        return subscription

    def test_sl46_an_active_subscription_is_returned(self):
        """SL-46."""
        subscription = subscription_service.activate(self.tenant, self.plan)

        self.assertEqual(
            subscription_service.current_or_expired(self.tenant).pk, subscription.pk
        )

    def test_sl47_a_lapsed_subscription_is_still_the_tenants_record(self):
        """SL-47: **the relationship did not end with the cycle.**

        Returning ``None`` here is what left an expired tenant unable to see why
        they were blocked or what it would cost to come back.
        """
        subscription = self.lapse(
            subscription_service.activate(self.tenant, self.plan)
        )

        found = subscription_service.current_or_expired(self.tenant)

        self.assertIsNotNone(found)
        self.assertEqual(found.pk, subscription.pk)
        self.assertEqual(found.status, Status.EXPIRED)

    def test_sl48_the_most_recent_lapsed_cycle_wins(self):
        """SL-48."""
        older = self.lapse(subscription_service.activate(self.tenant, self.plan))
        Subscription.objects.filter(pk=older.pk).update(
            end_date=timezone.now() - timedelta(days=400)
        )
        newer = self.lapse(subscription_service.activate(self.tenant, self.bigger))

        self.assertEqual(
            subscription_service.current_or_expired(self.tenant).pk, newer.pk
        )

    def test_sl49_cancelled_rows_are_not_the_tenants_record(self):
        """SL-49: **those were superseded by a plan change**, so the row that
        replaced them is the one that matters. Offering a cancelled row back would
        quote the tenant terms they already moved off."""
        old = subscription_service.activate(self.tenant, self.plan)
        subscription_service.apply_plan_change(old, self.bigger)
        old.refresh_from_db()
        self.assertEqual(old.status, Status.CANCELLED)

        # Remove the replacement, leaving only the cancelled row behind.
        Subscription.objects.filter(
            tenant=self.tenant, status=Status.ACTIVE
        ).update(status=Status.CANCELLED)

        self.assertIsNone(subscription_service.current_or_expired(self.tenant))

    def test_sl50_an_active_row_beats_an_older_lapsed_one(self):
        """SL-50."""
        stale = self.lapse(subscription_service.activate(self.tenant, self.plan))
        live = subscription_service.activate(self.tenant, self.bigger)

        found = subscription_service.current_or_expired(self.tenant)

        self.assertEqual(found.pk, live.pk)
        self.assertNotEqual(found.pk, stale.pk)

    def test_sl51_a_tenant_who_never_subscribed_has_no_record(self):
        """SL-51: distinct from 'lapsed', and the API turns the two into different
        error codes."""
        fresh = make_tenant("never-subscribed")

        self.assertIsNone(subscription_service.current_or_expired(fresh.tenant))

    def test_sl52_reactivating_closes_the_lapsed_row_and_links_it(self):
        """SL-52: coming back on a different plan is a plan change like any other,
        started from an expired row rather than a live one."""
        lapsed = self.lapse(
            subscription_service.activate(self.tenant, self.plan)
        )

        new = subscription_service.apply_plan_change(lapsed, self.bigger)

        lapsed.refresh_from_db()
        self.assertEqual(lapsed.status, Status.CANCELLED)
        self.assertEqual(lapsed.superseded_by_id, new.pk)
        self.assertEqual(new.status, Status.ACTIVE)
        self.assertEqual(new.plan_id, self.bigger.pk)

    def test_sl53_reactivation_grants_the_new_plans_allocation(self):
        """SL-53."""
        lapsed = self.lapse(
            subscription_service.activate(self.tenant, self.plan)
        )

        subscription_service.apply_plan_change(lapsed, self.bigger)

        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.allocated_balance, 3000)

    def test_sl54_reactivation_restores_spending(self):
        """SL-54: a tenant who has just paid and still cannot place a call has, as
        far as they can tell, not been reactivated at all."""
        lapsed = self.lapse(
            subscription_service.activate(self.tenant, self.plan)
        )
        self.assertTrue(self.wallet.is_frozen)

        subscription_service.apply_plan_change(lapsed, self.bigger)
        wallet_service.unfreeze(self.tenant)

        entry = wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_HIGH_LATENCY,
            quantity=60, reference_id="call-after-reactivation",
        )

        self.assertIsNotNone(entry)
        self.assertEqual(abs(entry.amount), 3)

    def test_sl55_purchased_credits_survive_the_whole_lapse_and_return(self):
        """SL-55: **the end-to-end guarantee**, across an expiry, a freeze, a plan
        change and a fresh allocation."""
        subscription = subscription_service.activate(self.tenant, self.plan)
        self.top_up(250)

        lapsed = self.lapse(subscription)
        self.assertEqual(self.wallet.purchased_balance, 250)

        subscription_service.apply_plan_change(lapsed, self.bigger)

        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.purchased_balance, 250)
        self.assertEqual(wallet.allocated_balance, 3000)
        self.assertEqual(wallet.balance, 3250)
