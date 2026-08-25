"""Section 7 of the billing test plan — WT-01 to WT-16.

Three scheduled jobs, none of which is allowed to move money. Two of them exist
purely to notice that something already went wrong: an uncharged call is revenue
quietly not collected, and a drifted balance is a number somebody has already been
shown.
"""

from datetime import timedelta

from django.conf import settings
from django.test import TestCase
from django.utils import timezone

from core.models.in_app_notification import InAppNotification
from onboard.tests.billing_factories import (
    assert_invariant,
    make_agent,
    make_call_log,
    make_plan,
    make_tenant,
)
from wallet.models import CreditLedger, CreditRate, Wallet
from wallet.services import wallet as wallet_service
from wallet.tasks import (
    GRACE_MINUTES,
    reconcile_wallets,
    report_unrated_calls,
    send_low_balance_alerts,
)

Bucket = CreditLedger.Bucket
EntryType = CreditLedger.EntryType
Operation = CreditRate.Operation


def _age(instance, **delta):
    """Backdate ``created_at``, which is ``auto_now_add`` and cannot be set on create."""
    type(instance).objects.filter(pk=instance.pk).update(
        created_at=timezone.now() - timedelta(**delta)
    )
    instance.refresh_from_db()
    return instance


class UnratedCallReportTests(TestCase):
    """WT-01 to WT-06 — finding calls that completed but were never charged."""

    def setUp(self):
        self.ctx = make_tenant("unrated")
        self.tenant = self.ctx.tenant
        self.plan = make_plan(
            "unrated-plan", rates={Operation.CONVERSATION_HIGH_LATENCY: 5}
        )
        wallet_service.set_plan_context(self.tenant, self.plan)
        wallet_service.grant(
            self.tenant, entry_type=EntryType.CYCLE_ALLOCATION,
            amount=1000, reference_id="unrated-seed",
        )
        self.agent = make_agent(self.tenant)

    def _call(self, **overrides):
        call = make_call_log(self.tenant, self.agent, **overrides)
        return _age(call, minutes=GRACE_MINUTES + 5)

    def test_wt01_a_completed_uncharged_call_is_reported(self):
        """WT-01: **revenue quietly not collected**, and this is the only way to
        notice it."""
        self._call(duration_seconds=90)

        with self.assertLogs("wallet.tasks.unrated_calls", level="ERROR") as logs:
            count = report_unrated_calls()

        self.assertEqual(count, 1)
        self.assertIn("unrated call", "".join(logs.output))

    def test_wt02_a_charged_call_is_not_reported(self):
        """WT-02: the join is on ``provider_call_id``, which is the same key the
        deduction was written under."""
        call = self._call(duration_seconds=90)
        wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_HIGH_LATENCY,
            quantity=call.duration_seconds, reference_id=call.provider_call_id,
        )

        self.assertEqual(report_unrated_calls(), 0)

    def test_wt03_a_zero_duration_call_is_not_reported(self):
        """WT-03: nothing was billable, so nothing is missing. Reporting these
        would bury the real findings in noise."""
        self._call(duration_seconds=0)

        self.assertEqual(report_unrated_calls(), 0)

    def test_wt04_a_call_inside_the_grace_window_is_not_reported(self):
        """WT-04: the request that charges it may still be in flight. Alerting on
        a call from thirty seconds ago is alerting on normal operation."""
        make_call_log(self.tenant, self.agent, duration_seconds=90)

        self.assertEqual(report_unrated_calls(), 0)

    def test_wt05_a_call_older_than_the_lookback_is_not_reported(self):
        """WT-05: the sweep is a daily window, not an ever-growing backlog that
        re-reports the same historical gap forever."""
        call = make_call_log(self.tenant, self.agent, duration_seconds=90)
        _age(call, days=3)

        self.assertEqual(report_unrated_calls(hours=24), 0)

    def test_wt06_the_sweep_never_charges_anything(self):
        """WT-06: **reports only.**

        Charging retroactively would bill the tenant days later at whatever rate is
        stamped now, which is not the rate that applied when the call happened.
        """
        self._call(duration_seconds=90)
        before = CreditLedger.all_objects.filter(tenant=self.tenant).count()
        balance_before = Wallet.objects.get(tenant=self.tenant).balance

        with self.assertLogs("wallet.tasks.unrated_calls", level="ERROR"):
            report_unrated_calls()

        self.assertEqual(
            CreditLedger.all_objects.filter(tenant=self.tenant).count(), before
        )
        self.assertEqual(
            Wallet.objects.get(tenant=self.tenant).balance, balance_before
        )


class ReconciliationTests(TestCase):
    """WT-07 to WT-10 — the scheduled assertion that balances match their ledgers."""

    def setUp(self):
        self.ctx = make_tenant("reconcile")
        self.tenant = self.ctx.tenant
        self.plan = make_plan("reconcile-plan")
        wallet_service.set_plan_context(self.tenant, self.plan)
        wallet_service.grant(
            self.tenant, entry_type=EntryType.CYCLE_ALLOCATION,
            amount=1000, reference_id="recon-allocated",
        )
        wallet_service.grant(
            self.tenant, entry_type=EntryType.MANUAL_TOP_UP,
            amount=500, bucket=Bucket.PURCHASED, reference_id="recon-purchased",
        )

    def drift(self, **fields):
        """Write a balance the ledger does not support.

        Goes through ``update()`` deliberately: the service cannot produce this
        state, which is the point — the job has to catch a wrong number however it
        got there.
        """
        Wallet.objects.filter(tenant=self.tenant).update(**fields)

    def test_wt07_a_healthy_platform_reconciles_cleanly(self):
        """WT-07."""
        assert_invariant(self, self.tenant)

        self.assertEqual(reconcile_wallets(), 0)

    def test_wt08_a_drifted_total_is_detected_and_named(self):
        """WT-08: the wallet id has to be in the log, because 'something drifted'
        is not something anyone can act on."""
        self.drift(allocated_balance=900, balance=1400)
        wallet = Wallet.objects.get(tenant=self.tenant)

        with self.assertLogs("wallet.tasks.reconciliation", level="ERROR"):
            result = reconcile_wallets()

        self.assertNotEqual(result, 0)

        from io import StringIO

        from django.core.management import call_command

        errors = StringIO()
        with self.assertRaises(SystemExit):
            call_command("reconcile_wallets", stderr=errors)
        self.assertIn(str(wallet.pk), errors.getvalue())

    def test_wt09_buckets_that_drift_while_the_total_agrees_are_caught(self):
        """WT-09: **the drift that hides.**

        A charge booked against the wrong bucket keeps the total correct, so a
        total-only check passes while the tenant's purchased credits quietly become
        ones that expire at the next renewal.
        """
        self.drift(allocated_balance=500, purchased_balance=1000, balance=1500)

        with self.assertLogs("wallet.tasks.reconciliation", level="ERROR"):
            self.assertNotEqual(reconcile_wallets(), 0)

    def test_wt10_the_scheduled_run_never_repairs(self):
        """WT-10: auto-correcting on a timer hides the bug that caused the drift,
        and you would never learn the balance had been wrong. ``--fix`` is an
        operator's decision, not a cron job's."""
        self.drift(allocated_balance=900, balance=1400)

        with self.assertLogs("wallet.tasks.reconciliation", level="ERROR"):
            reconcile_wallets()

        wallet = Wallet.objects.get(tenant=self.tenant)
        self.assertEqual(wallet.balance, 1400)
        self.assertEqual(wallet.allocated_balance, 900)
        self.assertFalse(
            CreditLedger.all_objects.filter(
                tenant=self.tenant, entry_type=EntryType.ADMIN_ADJUSTMENT
            ).exists()
        )


class LowBalanceAlertTests(TestCase):
    """WT-11 to WT-15 — warning a tenant before the credits run out."""

    def setUp(self):
        self.ctx = make_tenant("low-balance")
        self.tenant = self.ctx.tenant
        self.plan = make_plan("low-balance-plan", low_balance_threshold=100)
        wallet_service.set_plan_context(self.tenant, self.plan)

    def fund(self, amount, reference="low-seed"):
        if amount:
            wallet_service.grant(
                self.tenant, entry_type=EntryType.CYCLE_ALLOCATION,
                amount=amount, reference_id=reference,
            )

    def notifications(self):
        return InAppNotification.objects.filter(tenant=self.tenant)

    def test_wt11_a_wallet_at_the_threshold_is_warned(self):
        """WT-11: 'at or below', so the alert lands before the balance is already
        past the point it was warning about."""
        self.fund(100)

        sent = send_low_balance_alerts()

        self.assertEqual(sent, 1)
        self.assertEqual(self.notifications().count(), 1)
        self.assertIn("credits remaining", self.notifications().first().title)

    def test_wt12_a_comfortable_wallet_is_left_alone(self):
        """WT-12."""
        self.fund(5000)

        self.assertEqual(send_low_balance_alerts(), 0)
        self.assertEqual(self.notifications().count(), 0)

    def test_wt13_a_threshold_of_zero_means_alerting_is_off(self):
        """WT-13: otherwise every empty wallet on the platform alerts every day,
        forever, including ones nobody is using."""
        Wallet.objects.filter(tenant=self.tenant).update(low_balance_threshold=0)

        self.assertEqual(send_low_balance_alerts(), 0)
        self.assertEqual(self.notifications().count(), 0)

    def test_wt14_a_second_run_inside_the_quiet_window_says_nothing(self):
        """WT-14: a tenant hovering near their threshold would otherwise be told
        the same thing every day until they stopped reading any of it."""
        self.fund(50)
        send_low_balance_alerts()

        self.assertEqual(send_low_balance_alerts(), 0)
        self.assertEqual(self.notifications().count(), 1)

    def test_wt15_a_frozen_wallet_is_skipped(self):
        """WT-15: they already know — the wallet is frozen. 'You are low on
        credits' on top of that is noise on a worse message."""
        self.fund(50)
        wallet_service.freeze(self.tenant, reason="subscription_expired")

        self.assertEqual(send_low_balance_alerts(), 0)
        self.assertEqual(self.notifications().count(), 0)


class TaskNameTests(TestCase):
    """WT-16 — the names beat refers to."""

    def test_wt16_every_task_name_is_explicit_and_scheduled(self):
        """WT-16: **explicit ``name=`` is why moving a task between files is safe.**

        With an auto-derived name, splitting ``tasks.py`` into a package renames
        every task and orphans every schedule entry — silently, because beat simply
        stops finding them.
        """
        expected = {
            report_unrated_calls: "wallet.tasks.report_unrated_calls",
            reconcile_wallets: "wallet.tasks.reconcile_wallets",
            send_low_balance_alerts: "wallet.tasks.send_low_balance_alerts",
        }

        for task, name in expected.items():
            self.assertEqual(task.name, name)

        scheduled = {
            entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()
        }
        for name in expected.values():
            self.assertIn(
                name, scheduled, f"{name} is not on the beat schedule"
            )
