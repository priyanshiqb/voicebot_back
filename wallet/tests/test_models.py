"""Section 1 of the billing test plan — WM-01 to WM-24.

The two catalogue-and-state models, tested at the database rather than through a
service. Every constraint here exists because a service bug would otherwise produce a
wrong number that nobody notices until reconciliation, so the tests assert that the
*database* refuses it, not that some Python happens to check first.
"""

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from onboard.tests.billing_factories import make_plan, make_tenant
from wallet.models import CreditRate, Wallet


class WalletModelTests(TestCase):
    """WM-01 to WM-14 — the balance row itself."""

    def setUp(self):
        self.ctx = make_tenant("wallet-model")
        self.tenant = self.ctx.tenant

    # --- for_tenant ---------------------------------------------------------

    def test_wm01_for_tenant_creates_an_empty_wallet(self):
        """WM-01: a tenant that has never had a wallet gets one, at zero."""
        self.assertFalse(Wallet.objects.filter(tenant=self.tenant).exists())

        wallet = Wallet.for_tenant(self.tenant.id)

        self.assertIsNotNone(wallet.pk)
        self.assertEqual(wallet.balance, 0)
        self.assertEqual(wallet.allocated_balance, 0)
        self.assertEqual(wallet.purchased_balance, 0)

    def test_wm02_for_tenant_is_idempotent(self):
        """WM-02: called twice, one row. This runs on the call-authorisation path."""
        first = Wallet.for_tenant(self.tenant.id)
        second = Wallet.for_tenant(self.tenant.id)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Wallet.objects.filter(tenant=self.tenant).count(), 1)

    # --- uniqueness ---------------------------------------------------------

    def test_wm03_second_wallet_for_one_tenant_is_refused(self):
        """WM-03: uniq_wallet_per_tenant. Two wallets means two balances."""
        Wallet.objects.create(tenant=self.tenant)

        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                Wallet.objects.create(tenant=self.tenant)

        self.assertIn("uniq_wallet_per_tenant", str(caught.exception))

    def test_wm04_soft_deleted_wallet_does_not_block_a_replacement(self):
        """WM-04: the constraint is partial, so a deleted row frees the slot."""
        original = Wallet.objects.create(tenant=self.tenant, balance=0)
        original.soft_delete()

        replacement = Wallet.objects.create(tenant=self.tenant)

        self.assertNotEqual(replacement.pk, original.pk)
        self.assertEqual(Wallet.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(Wallet.all_objects.filter(tenant=self.tenant).count(), 2)

    # --- the bucket invariant, enforced by the database ---------------------

    def test_wm05_balance_must_equal_the_sum_of_its_buckets(self):
        """WM-05: ck_wallet_balance_is_sum_of_buckets.

        This is the constraint that catches a service writing a balance it did not
        derive from the buckets, at the moment it happens rather than at the next
        reconciliation run.
        """
        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                Wallet.objects.create(
                    tenant=self.tenant,
                    balance=100,
                    allocated_balance=60,
                    purchased_balance=30,
                )

        self.assertIn("ck_wallet_balance_is_sum_of_buckets", str(caught.exception))

    def test_wm06_consistent_buckets_are_accepted(self):
        """WM-06: the same write, with the arithmetic right."""
        wallet = Wallet.objects.create(
            tenant=self.tenant,
            balance=90,
            allocated_balance=60,
            purchased_balance=30,
        )

        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 90)

    def test_wm07_negative_balance_is_refused(self):
        """WM-07: PositiveIntegerField. A negative balance is credit we never sold."""
        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                Wallet.objects.create(
                    tenant=self.tenant,
                    balance=-5,
                    allocated_balance=-5,
                    purchased_balance=0,
                )

        self.assertIn("balance", str(caught.exception))

    # --- is_spendable -------------------------------------------------------

    def test_wm08_spendable_when_funded_and_not_frozen(self):
        """WM-08."""
        wallet = Wallet.objects.create(
            tenant=self.tenant, balance=10, allocated_balance=10
        )
        self.assertTrue(wallet.is_spendable)

    def test_wm09_frozen_wallet_is_not_spendable(self):
        """WM-09: a balance the tenant cannot spend is still a balance they own."""
        wallet = Wallet.objects.create(
            tenant=self.tenant, balance=10, allocated_balance=10, is_frozen=True
        )
        self.assertFalse(wallet.is_spendable)

    def test_wm10_empty_wallet_is_not_spendable(self):
        """WM-10."""
        wallet = Wallet.objects.create(tenant=self.tenant, balance=0)
        self.assertFalse(wallet.is_spendable)

    # --- is_low -------------------------------------------------------------

    def test_wm11_low_below_the_threshold(self):
        """WM-11."""
        wallet = Wallet.objects.create(
            tenant=self.tenant, balance=5, allocated_balance=5, low_balance_threshold=10
        )
        self.assertTrue(wallet.is_low)

    def test_wm12_low_at_the_threshold_not_only_below_it(self):
        """WM-12: 'at or below'. An alert that fires one credit late is an alert
        that fires after the call it was meant to warn about."""
        wallet = Wallet.objects.create(
            tenant=self.tenant,
            balance=10,
            allocated_balance=10,
            low_balance_threshold=10,
        )
        self.assertTrue(wallet.is_low)

    def test_wm13_threshold_of_zero_disables_the_alert(self):
        """WM-13: 0 means off, including at a balance of 0 — otherwise every empty
        wallet on the platform alerts every day forever."""
        empty = Wallet.objects.create(
            tenant=self.tenant, balance=0, low_balance_threshold=0
        )
        self.assertFalse(empty.is_low)

        funded = make_tenant("threshold-off")
        wallet = Wallet.objects.create(
            tenant=funded.tenant,
            balance=500,
            allocated_balance=500,
            low_balance_threshold=0,
        )
        self.assertFalse(wallet.is_low)

    def test_wm14_str_names_the_tenant_and_the_balance(self):
        """WM-14: staff read this in the admin changelist."""
        wallet = Wallet.objects.create(
            tenant=self.tenant, balance=250, allocated_balance=250
        )

        rendered = str(wallet)
        self.assertIn(self.tenant.name, rendered)
        self.assertIn("250", rendered)


class CreditRateModelTests(TestCase):
    """WM-15 to WM-24 — the rate catalogue.

    Rates are superseded, never edited. The constraints below are what make that
    stick; without them a plan can end up with two current prices and stamping
    becomes a coin flip.
    """

    def setUp(self):
        # No rate card: these tests build their own, and the factory's default card
        # would occupy the very slots being tested.
        self.plan = make_plan("rate-tests", with_rates=False)
        self.other_plan = make_plan("rate-tests-other", with_rates=False)
        self.now = timezone.now()
        self.operation = CreditRate.Operation.CONVERSATION_HIGH_LATENCY

    def _rate(self, **overrides):
        fields = {
            "plan": self.plan,
            "operation": self.operation,
            "unit": CreditRate.Unit.PER_MINUTE,
            "credits_per_unit": 8,
            "effective_from": self.now - timedelta(days=30),
        }
        fields.update(overrides)
        return CreditRate.objects.create(**fields)

    # --- one current rate per plan and operation ----------------------------

    def test_wm15_two_open_ended_rates_collide(self):
        """WM-15: uniq_current_rate_per_plan_operation."""
        self._rate()

        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                self._rate(credits_per_unit=9)

        self.assertIn("uniq_current_rate_per_plan_operation", str(caught.exception))

    def test_wm16_a_superseded_rate_sits_beside_the_current_one(self):
        """WM-16: this is what re-pricing looks like, and it must be allowed."""
        old = self._rate(effective_to=self.now - timedelta(days=1))
        new = self._rate(credits_per_unit=9, effective_from=self.now - timedelta(days=1))

        self.assertFalse(old.is_current)
        self.assertTrue(new.is_current)
        self.assertEqual(
            CreditRate.objects.filter(
                plan=self.plan, operation=self.operation
            ).count(),
            2,
        )

    def test_wm17_the_same_operation_is_priced_per_plan(self):
        """WM-17: the whole point of plan-scoped rates — Pro talks cheaper."""
        self._rate(credits_per_unit=8)
        self._rate(plan=self.other_plan, credits_per_unit=5)

        self.assertEqual(
            CreditRate.objects.filter(operation=self.operation).count(), 2
        )

    # --- period validity ----------------------------------------------------

    def test_wm18_a_zero_length_window_is_refused(self):
        """WM-18: ck_credit_rate_period_valid. A rate that never applied would be
        stamped by nothing and explain nothing."""
        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                self._rate(effective_from=self.now, effective_to=self.now)

        self.assertIn("ck_credit_rate_period_valid", str(caught.exception))

    def test_wm19_a_backwards_window_is_refused(self):
        """WM-19."""
        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                self._rate(
                    effective_from=self.now,
                    effective_to=self.now - timedelta(days=1),
                )

        self.assertIn("ck_credit_rate_period_valid", str(caught.exception))

    def test_wm20_an_open_ended_rate_is_the_current_one(self):
        """WM-20."""
        rate = self._rate(effective_to=None)

        self.assertIsNone(rate.effective_to)
        self.assertTrue(rate.is_current)

    def test_wm21_soft_deleting_the_current_rate_frees_the_slot(self):
        """WM-21: the constraint is partial on is_deleted, so a withdrawn rate does
        not permanently block re-pricing."""
        original = self._rate()
        original.soft_delete()

        replacement = self._rate(credits_per_unit=11)

        self.assertNotEqual(replacement.pk, original.pk)
        self.assertEqual(
            CreditRate.objects.filter(
                plan=self.plan, operation=self.operation, effective_to__isnull=True
            ).count(),
            1,
        )

    # --- the two things the rating service reads off a rate -----------------

    def test_wm22_unit_divisor_per_unit_type(self):
        """WM-22: 60 seconds to a minute, 1 to a second, and per-event ignores
        quantity entirely — which is why it has no divisor rather than a divisor
        of 1."""
        per_minute = self._rate(unit=CreditRate.Unit.PER_MINUTE)
        per_second = self._rate(
            operation=CreditRate.Operation.STATIC_REEL,
            unit=CreditRate.Unit.PER_SECOND,
        )
        per_event = self._rate(
            operation=CreditRate.Operation.CONVERSATION_LOW_LATENCY,
            unit=CreditRate.Unit.PER_EVENT,
        )

        self.assertEqual(per_minute.unit_divisor, 60)
        self.assertEqual(per_second.unit_divisor, 1)
        self.assertIsNone(per_event.unit_divisor)

    def test_wm23_snapshot_entry_carries_exactly_what_rating_reads(self):
        """WM-23: the stamped JSON shape. An extra key is harmless; a missing one
        breaks rating for every wallet stamped after the change."""
        rate = self._rate(
            credits_per_unit=8,
            rounding=CreditRate.Rounding.CEIL,
            minimum_units=2,
        )

        entry = rate.as_snapshot_entry()

        self.assertEqual(
            set(entry),
            {"unit", "credits_per_unit", "rounding", "minimum_units", "rate_id"},
        )
        self.assertEqual(entry["unit"], CreditRate.Unit.PER_MINUTE)
        self.assertEqual(entry["credits_per_unit"], 8)
        self.assertEqual(entry["rounding"], CreditRate.Rounding.CEIL)
        self.assertEqual(entry["minimum_units"], 2)
        self.assertEqual(entry["rate_id"], str(rate.id))

    def test_wm24_a_rate_must_belong_to_a_plan(self):
        """WM-24: there is no platform-wide fallback rate, deliberately. One would
        turn 'which rate did this tenant get?' into an investigation."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CreditRate.objects.create(
                    plan=None,
                    operation=self.operation,
                    credits_per_unit=8,
                    effective_from=self.now,
                )
