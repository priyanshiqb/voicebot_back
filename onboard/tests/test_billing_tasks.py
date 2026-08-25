"""Section 15 of the billing test plan — BT-01 to BT-22.

The clock. Three jobs that move subscriptions and orders on without anyone clicking.

BT-02 is the reason ``process_billing_cycle`` is one task rather than two: an expiry
job and a scheduled-change job race at exactly the moment both fire, and the loser is
a tenant who scheduled a downgrade and got expired instead — losing access they had
paid to keep.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase
from django.utils import timezone
from rest_framework import status

from core.models.in_app_notification import InAppNotification
from onboard.models import Subscription, Transaction
from onboard.services import subscriptions as subscription_service
from onboard.tasks import (
    REMINDER_DAYS,
    expire_stale_billing_orders,
    process_billing_cycle,
    send_renewal_reminders,
)
from onboard.tests.billing_factories import (
    BILLING,
    make_free_plan,
    make_plan,
    make_tenant,
    make_transaction,
)
from wallet.models import Wallet

Status = Subscription.SubscriptionStatusChoices
TxnStatus = Transaction.TransactionStatusChoices
TxnType = Transaction.TransactionTypeChoices

TOP_UP_URL = f"{BILLING}/top-up/"


def lapse(subscription, days=1):
    """Push a cycle into the past without running any lifecycle code."""
    Subscription.objects.filter(pk=subscription.pk).update(
        start_date=timezone.now() - timedelta(days=30 + days),
        end_date=timezone.now() - timedelta(days=days),
    )
    subscription.refresh_from_db()
    return subscription


class BillingCycleTests(TestCase):
    """BT-01 to BT-10, BT-22."""

    def setUp(self):
        self.plan = make_plan("cycle-task-plan", allocated_credits=1000)
        self.cheaper = make_plan(
            "cycle-task-cheaper",
            base_price=Decimal("100.00"),
            allocated_credits=100,
        )

    def subscribe(self, name="cycle-task"):
        ctx = make_tenant(name)
        return ctx, subscription_service.activate(ctx, self.plan)

    def ledger_forfeits(self, tenant):
        from wallet.models import CreditLedger

        return CreditLedger.all_objects.filter(
            tenant=tenant, entry_type=CreditLedger.EntryType.EXPIRY_FORFEIT
        ).count()

    def test_bt01_a_due_scheduled_change_is_applied(self):
        """BT-01: the downgrade the tenant asked for, on the day they asked for."""
        ctx, subscription = self.subscribe()
        subscription_service.schedule_plan_change(subscription, self.cheaper)
        Subscription.objects.filter(pk=subscription.pk).update(
            scheduled_change_at=timezone.now() - timedelta(minutes=1)
        )

        result = process_billing_cycle()

        live = subscription_service.current_for(ctx.tenant)
        self.assertEqual(live.plan_id, self.cheaper.pk)
        self.assertEqual(live.status, Status.ACTIVE)
        self.assertEqual(result["changed"], 1)

    def test_bt02_a_due_change_wins_over_expiry(self):
        """BT-02: **the ordering rule, and the reason this is one task.**

        The cycle has ended *and* a downgrade is due. Expiring would freeze the
        wallet and forfeit the allocation of a tenant who had arranged to continue
        on a cheaper plan.
        """
        ctx, subscription = self.subscribe("cycle-task-both")
        subscription_service.schedule_plan_change(subscription, self.cheaper)
        lapse(subscription)
        Subscription.objects.filter(pk=subscription.pk).update(
            scheduled_change_at=timezone.now() - timedelta(minutes=1)
        )

        result = process_billing_cycle()

        live = subscription_service.current_for(ctx.tenant)
        self.assertIsNotNone(live, "the tenant must not have been expired")
        self.assertEqual(live.plan_id, self.cheaper.pk)
        self.assertEqual(result["expired"], 0)
        self.assertFalse(Wallet.objects.get(tenant=ctx.tenant).is_frozen)

    def test_bt03_a_lapsed_subscription_with_no_plans_is_expired(self):
        """BT-03."""
        ctx, subscription = self.subscribe("cycle-task-lapsed")
        lapse(subscription)

        result = process_billing_cycle()

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Status.EXPIRED)
        self.assertEqual(result["expired"], 1)
        self.assertTrue(Wallet.objects.get(tenant=ctx.tenant).is_frozen)

    def test_bt04_a_running_cycle_is_left_alone(self):
        """BT-04."""
        ctx, subscription = self.subscribe("cycle-task-running")

        result = process_billing_cycle()

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Status.ACTIVE)
        self.assertEqual(result, {"changed": 0, "renewed": 0, "expired": 0, "failed": 0})

    def test_bt05_a_plan_that_renews_itself_rolls_rather_than_lapsing(self):
        """BT-05: **what separates a trial that is meant to end from
        pay-as-you-go, which must not.** A zero-cost plan has nothing to charge, so
        the cycle simply rolls."""
        ctx = make_tenant("cycle-task-free")
        free = make_free_plan("cycle-task-free-plan", allocated_credits=100)
        subscription = subscription_service.activate(ctx, free)
        lapse(subscription)

        result = process_billing_cycle()

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Status.ACTIVE)
        self.assertGreater(subscription.end_date, timezone.now())
        self.assertEqual(result["renewed"], 1)
        self.assertEqual(result["expired"], 0)

    def test_bt06_one_bad_subscription_does_not_stop_the_sweep(self):
        """BT-06."""
        first, first_subscription = self.subscribe("cycle-task-a")
        second, second_subscription = self.subscribe("cycle-task-b")
        lapse(first_subscription)
        lapse(second_subscription)

        real_expire = subscription_service.expire
        seen = []

        def explode_once(subscription, *args, **kwargs):
            seen.append(subscription.pk)
            if len(seen) == 1:
                raise RuntimeError("this one is cursed")
            return real_expire(subscription, *args, **kwargs)

        with patch.object(subscription_service, "expire", side_effect=explode_once):
            with self.assertLogs("onboard.tasks.billing_cycle", level="ERROR"):
                result = process_billing_cycle()

        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["expired"], 1)

    def test_bt07_a_failure_rolls_back_only_its_own_subscription(self):
        """BT-07: **each subscription in its own transaction.**

        Without that, a bad row late in the sweep would undo every expiry that had
        already succeeded, and the next run would redo them all.
        """
        good, good_subscription = self.subscribe("cycle-task-good")
        bad, bad_subscription = self.subscribe("cycle-task-bad")
        lapse(good_subscription)
        lapse(bad_subscription)

        real_expire = subscription_service.expire
        order = []

        def explode_on_second(subscription, *args, **kwargs):
            order.append(subscription.pk)
            if len(order) == 2:
                raise RuntimeError("failed after the first was committed")
            return real_expire(subscription, *args, **kwargs)

        with patch.object(subscription_service, "expire", side_effect=explode_on_second):
            with self.assertLogs("onboard.tasks.billing_cycle", level="ERROR"):
                process_billing_cycle()

        statuses = set(
            Subscription.objects.filter(
                pk__in=[good_subscription.pk, bad_subscription.pk]
            ).values_list("status", flat=True)
        )
        self.assertEqual(statuses, {Status.EXPIRED, Status.ACTIVE})

    def test_bt08_a_second_run_changes_nothing(self):
        """BT-08: beat can fire twice, and a hung worker can overlap its successor."""
        ctx, subscription = self.subscribe("cycle-task-idempotent")
        lapse(subscription)
        process_billing_cycle()
        wallet_before = Wallet.objects.get(tenant=ctx.tenant).balance

        result = process_billing_cycle()

        self.assertEqual(result, {"changed": 0, "renewed": 0, "expired": 0, "failed": 0})
        self.assertEqual(Wallet.objects.get(tenant=ctx.tenant).balance, wallet_before)

    def test_bt09_the_return_value_counts_each_outcome(self):
        """BT-09: what a monitoring dashboard reads."""
        expiring, expiring_subscription = self.subscribe("cycle-task-count-a")
        lapse(expiring_subscription)

        renewing = make_tenant("cycle-task-count-b")
        free = make_free_plan("cycle-task-count-free")
        lapse(subscription_service.activate(renewing, free))

        changing, changing_subscription = self.subscribe("cycle-task-count-c")
        subscription_service.schedule_plan_change(changing_subscription, self.cheaper)
        Subscription.objects.filter(pk=changing_subscription.pk).update(
            scheduled_change_at=timezone.now() - timedelta(minutes=1)
        )

        result = process_billing_cycle()

        self.assertEqual(result["expired"], 1)
        self.assertEqual(result["renewed"], 1)
        self.assertEqual(result["changed"], 1)
        self.assertEqual(result["failed"], 0)

    def test_bt10_a_row_another_worker_already_took_is_skipped(self):
        """BT-10: **the sweep scans ids first, then re-reads each row under the
        lock.**

        Two workers firing together both see the same lapsed subscription in the
        scan. The second must find it already settled and leave it alone, rather
        than expiring it a second time and forfeiting a fresh allocation.

        The steal is simulated at the lock itself: the row changes hands in the
        window between the scan and the ``select_for_update``.
        """
        ctx, subscription = self.subscribe("cycle-task-raced")
        lapse(subscription)

        real_select_for_update = Subscription.objects.select_for_update

        def stolen_first(*args, **kwargs):
            Subscription.objects.filter(pk=subscription.pk).update(
                status=Status.EXPIRED
            )
            return real_select_for_update(*args, **kwargs)

        with patch.object(
            Subscription.objects, "select_for_update", side_effect=stolen_first
        ):
            result = process_billing_cycle()

        self.assertEqual(result["expired"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(
            self.ledger_forfeits(ctx.tenant), 0,
            "a second expiry would forfeit credits that were already forfeited",
        )

    def test_bt22_a_tenant_with_no_wallet_does_not_crash_the_sweep(self):
        """BT-22: wallets are created on demand, so a subscription written before
        the wallet app existed genuinely reaches expiry without one.

        Built by hand rather than through ``activate()`` precisely so no wallet is
        created — which is the state every pre-existing row on this platform was in
        before the backfill.
        """
        ctx = make_tenant("cycle-task-walletless")
        subscription = Subscription.objects.create(tenant=ctx.tenant, plan=self.plan)
        lapse(subscription)
        self.assertFalse(Wallet.all_objects.filter(tenant=ctx.tenant).exists())

        result = process_billing_cycle()

        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["expired"], 1)
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Status.EXPIRED)


class StaleOrderTests(TestCase):
    """BT-11 to BT-14."""

    def setUp(self):
        self.ctx = make_tenant("stale-orders")
        self.tenant = self.ctx.tenant
        self.plan = make_plan(
            "stale-orders-plan", cost_per_additional_credit=Decimal("1.00")
        )
        subscription_service.activate(self.tenant, self.plan)

    def order(self, *, expires_in, **overrides):
        fields = {
            "status": TxnStatus.PENDING,
            "transaction_type": TxnType.TOP_UP,
            "expires_at": timezone.now() + expires_in,
        }
        fields.update(overrides)
        return make_transaction(self.tenant, self.plan, **fields)

    def test_bt11_an_order_past_its_ttl_is_cancelled(self):
        """BT-11: **a single closed checkout tab would otherwise lock the tenant
        out of buying anything**, and the support request looks like a bug in the
        payment page."""
        stale = self.order(expires_in=-timedelta(minutes=5))

        self.assertEqual(expire_stale_billing_orders(), 1)
        stale.refresh_from_db()
        self.assertEqual(stale.status, TxnStatus.CANCELED)

    def test_bt12_an_order_still_within_its_ttl_is_left_alone(self):
        """BT-12: the tenant may be on the payment page right now."""
        fresh = self.order(expires_in=timedelta(minutes=20))

        self.assertEqual(expire_stale_billing_orders(), 0)
        fresh.refresh_from_db()
        self.assertEqual(fresh.status, TxnStatus.PENDING)

    def test_bt13_a_paid_order_is_never_touched(self):
        """BT-13: cancelling a settled order would contradict money that was taken."""
        paid = self.order(
            expires_in=-timedelta(hours=2), status=TxnStatus.SUCCESSFUL
        )

        expire_stale_billing_orders()

        paid.refresh_from_db()
        self.assertEqual(paid.status, TxnStatus.SUCCESSFUL)

    def test_bt14_the_tenant_can_buy_again_after_the_sweep(self):
        """BT-14: the whole point — the one-open-order rule has to be escapable."""
        self.order(expires_in=-timedelta(minutes=5))

        expire_stale_billing_orders()
        response = self.ctx.client.post(
            TOP_UP_URL, {"credits": 100}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)


class RenewalReminderTests(TestCase):
    """BT-15 to BT-19."""

    def setUp(self):
        self.plan = make_plan("reminder-plan")

    def subscribe_ending_in(self, days, name="reminder"):
        ctx = make_tenant(name)
        subscription = subscription_service.activate(ctx, self.plan)
        Subscription.objects.filter(pk=subscription.pk).update(
            end_date=timezone.now() + timedelta(days=days)
        )
        subscription.refresh_from_db()
        return ctx, subscription

    def notices(self, tenant):
        return InAppNotification.objects.filter(tenant=tenant)

    def test_bt15_a_reminder_goes_out_on_each_of_the_three_days(self):
        """BT-15: **renewals are manual — there is no auto-pay mandate.**

        A tenant who forgets simply loses access, which makes these the difference
        between a renewal and accidental churn.
        """
        contexts = [
            self.subscribe_ending_in(days, f"reminder-t{days}")[0]
            for days in REMINDER_DAYS
        ]

        sent = send_renewal_reminders()

        self.assertEqual(sent, len(REMINDER_DAYS))
        for ctx in contexts:
            self.assertEqual(self.notices(ctx.tenant).count(), 1)

    def test_bt16_no_reminder_on_a_day_that_is_not_one_of_the_three(self):
        """BT-16: one reminder is missed, five is nagging."""
        ctx, _ = self.subscribe_ending_in(5, "reminder-t5")

        self.assertEqual(send_renewal_reminders(), 0)
        self.assertEqual(self.notices(ctx.tenant).count(), 0)

    def test_bt17_running_twice_in_a_day_says_it_once(self):
        """BT-17: the task can be scheduled more than daily without meaning to say
        the same thing twice."""
        ctx, _ = self.subscribe_ending_in(3, "reminder-dedup")
        send_renewal_reminders()

        self.assertEqual(send_renewal_reminders(), 0)
        self.assertEqual(self.notices(ctx.tenant).count(), 1)

    def test_bt18_a_lapsed_subscription_is_not_reminded_to_renew_soon(self):
        """BT-18: they are past it — ``notify_expired`` is the message that fits."""
        ctx, subscription = self.subscribe_ending_in(3, "reminder-expired")
        Subscription.objects.filter(pk=subscription.pk).update(status=Status.EXPIRED)

        self.assertEqual(send_renewal_reminders(), 0)
        self.assertEqual(self.notices(ctx.tenant).count(), 0)

    def test_bt19_one_tenants_failure_does_not_silence_the_rest(self):
        """BT-19."""
        first, _ = self.subscribe_ending_in(7, "reminder-fail-a")
        second, _ = self.subscribe_ending_in(7, "reminder-fail-b")

        from onboard.services import billing_notifications

        real_notify = billing_notifications.notify_renewal_due
        calls = []

        def explode_once(subscription, **kwargs):
            calls.append(subscription.pk)
            if len(calls) == 1:
                raise RuntimeError("notification backend down")
            return real_notify(subscription, **kwargs)

        with patch.object(
            billing_notifications, "notify_renewal_due", side_effect=explode_once
        ):
            with self.assertLogs("onboard.tasks.reminders", level="ERROR"):
                sent = send_renewal_reminders()

        self.assertEqual(sent, 1)


class TaskWiringTests(TestCase):
    """BT-20 to BT-21 — the names beat refers to."""

    def test_bt20_every_billing_job_is_on_the_schedule(self):
        """BT-20: a task nobody scheduled is a task that never runs, and expiry
        silently stopping is not something anyone would notice quickly."""
        scheduled = {
            entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()
        }

        for name in (
            "onboard.tasks.process_billing_cycle",
            "onboard.tasks.expire_stale_billing_orders",
            "onboard.tasks.send_renewal_reminders",
            "wallet.tasks.send_low_balance_alerts",
            "wallet.tasks.report_unrated_calls",
            "wallet.tasks.reconcile_wallets",
        ):
            self.assertIn(name, scheduled, f"{name} is not on the beat schedule")

    def test_bt21_task_names_are_declared_rather_than_derived(self):
        """BT-21: **with an auto-derived name, splitting ``tasks.py`` into a
        package renames every task and orphans every schedule entry** — silently,
        because beat simply stops finding them."""
        self.assertEqual(
            process_billing_cycle.name, "onboard.tasks.process_billing_cycle"
        )
        self.assertEqual(
            expire_stale_billing_orders.name,
            "onboard.tasks.expire_stale_billing_orders",
        )
        self.assertEqual(
            send_renewal_reminders.name, "onboard.tasks.send_renewal_reminders"
        )
