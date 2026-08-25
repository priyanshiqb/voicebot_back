"""Section 5 of the billing test plan — WS-01 to WS-55.

``wallet.services.wallet`` is the only thing allowed to move a balance. Every test
here ends by asserting ``SUM(ledger) == balance`` per bucket, because a service that
updates a balance without writing a row — or writes a row against the wrong bucket —
passes its own targeted assertion and fails only that one.
"""

import random
import threading

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, TransactionTestCase

from onboard.tests.billing_factories import (
    add_rates,
    assert_invariant,
    make_plan,
    make_tenant,
    subscribe,
)
from tenant.models import TenantSettings
from wallet.models import CreditLedger, CreditRate, Wallet
from wallet.services import wallet as wallet_service
from wallet.services.exceptions import (
    InsufficientCredits,
    RateNotConfigured,
    WalletFrozen,
)

User = get_user_model()

Bucket = CreditLedger.Bucket
EntryType = CreditLedger.EntryType
Operation = CreditRate.Operation

#: Deliberately round numbers so a failure reads as arithmetic rather than as a
#: puzzle: 10 credits a minute means a 90-second call costs 20.
RATES = {
    Operation.STATIC_REEL: 0,
    Operation.CONVERSATION_HIGH_LATENCY: 5,
    Operation.CONVERSATION_LOW_LATENCY: 10,
}


class WalletServiceTestCase(TestCase):
    """A tenant on a plan with a known rate card and a stamped wallet."""

    def setUp(self):
        self.ctx = make_tenant("wallet-service")
        self.tenant = self.ctx.tenant
        self.plan = make_plan(
            "wallet-service-plan", rates=RATES, low_balance_threshold=100
        )
        wallet_service.set_plan_context(self.tenant, self.plan)

    # --- helpers ------------------------------------------------------------

    def fund(self, allocated=0, purchased=0, prefix="seed"):
        if allocated:
            wallet_service.grant(
                self.tenant, entry_type=EntryType.CYCLE_ALLOCATION,
                amount=allocated, bucket=Bucket.ALLOCATED,
                reference_id=f"{prefix}-allocated",
            )
        if purchased:
            wallet_service.grant(
                self.tenant, entry_type=EntryType.MANUAL_TOP_UP,
                amount=purchased, bucket=Bucket.PURCHASED,
                reference_id=f"{prefix}-purchased",
            )
        return self.wallet

    @property
    def wallet(self):
        return Wallet.objects.get(tenant=self.tenant)

    @property
    def rows(self):
        return CreditLedger.all_objects.filter(tenant=self.tenant)


class GrantTests(WalletServiceTestCase):
    """WS-01 to WS-10 — credits in."""

    def test_ws01_granting_to_the_allocated_bucket(self):
        """WS-01."""
        wallet_service.grant(
            self.tenant, entry_type=EntryType.CYCLE_ALLOCATION,
            amount=1000, reference_id="ws01",
        )

        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.balance, 1000)
        self.assertEqual(wallet.allocated_balance, 1000)
        self.assertEqual(wallet.purchased_balance, 0)

    def test_ws02_granting_to_the_purchased_bucket_leaves_allocated_alone(self):
        """WS-02: the two buckets are independent, and only one of them is subject
        to rollover rules."""
        self.fund(allocated=1000)

        wallet_service.grant(
            self.tenant, entry_type=EntryType.MANUAL_TOP_UP,
            amount=500, bucket=Bucket.PURCHASED, reference_id="ws02",
        )

        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.allocated_balance, 1000)
        self.assertEqual(wallet.purchased_balance, 500)
        self.assertEqual(wallet.balance, 1500)

    def test_ws03_the_row_records_the_balance_it_produced(self):
        """WS-03: ``balance_after`` is how a support agent reconstructs a timeline
        without re-summing the whole ledger."""
        self.fund(allocated=100)

        entry = wallet_service.grant(
            self.tenant, entry_type=EntryType.MANUAL_TOP_UP,
            amount=50, bucket=Bucket.PURCHASED, reference_id="ws03",
        )

        self.assertEqual(entry.amount, 50)
        self.assertEqual(entry.balance_after, 150)
        self.assertEqual(entry.balance_after, self.wallet.balance)

    def test_ws04_lifetime_granted_accumulates(self):
        """WS-04: monotonic, analytics only — never read to make a decision."""
        self.fund(allocated=1000, purchased=500)

        self.assertEqual(self.wallet.lifetime_granted, 1500)

    def test_ws05_a_replayed_grant_is_a_no_op(self):
        """WS-05: **Razorpay delivers the same event twice as a matter of course.**

        The second delivery must return quietly, not raise and not credit again.
        """
        first = wallet_service.grant(
            self.tenant, entry_type=EntryType.MANUAL_TOP_UP,
            amount=500, bucket=Bucket.PURCHASED, reference_id="pay_replayed",
        )
        before = self.wallet.balance

        second = wallet_service.grant(
            self.tenant, entry_type=EntryType.MANUAL_TOP_UP,
            amount=500, bucket=Bucket.PURCHASED, reference_id="pay_replayed",
        )

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(self.wallet.balance, before)
        self.assertEqual(
            self.rows.filter(reference_id="pay_replayed").count(), 1
        )
        assert_invariant(self, self.tenant)

    def test_ws06_unreferenced_grants_cannot_be_deduplicated(self):
        """WS-06: honest behaviour, not a gap. With no key there is nothing to
        compare against, which is why every replayable path supplies one."""
        wallet_service.grant(
            self.tenant, entry_type=EntryType.ADMIN_ADJUSTMENT, amount=10,
            reference_id=None,
        )
        wallet_service.grant(
            self.tenant, entry_type=EntryType.ADMIN_ADJUSTMENT, amount=10,
            reference_id=None,
        )

        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.balance, 20)

    def test_ws07_a_grant_of_zero_is_refused(self):
        """WS-07."""
        with self.assertRaises(ValueError):
            wallet_service.grant(
                self.tenant, entry_type=EntryType.CYCLE_ALLOCATION,
                amount=0, reference_id="ws07",
            )

    def test_ws08_a_negative_grant_is_refused(self):
        """WS-08: taking credits away has its own entry types, and going through
        ``grant`` would record the movement under a misleading reason."""
        with self.assertRaises(ValueError):
            wallet_service.grant(
                self.tenant, entry_type=EntryType.CYCLE_ALLOCATION,
                amount=-100, reference_id="ws08",
            )

    def test_ws09_a_grant_lands_on_a_frozen_wallet(self):
        """WS-09: **a renewal payment has to be able to land before the thing that
        unfreezes the wallet runs.** Refusing the deposit strands a tenant who has
        just paid us."""
        wallet_service.freeze(self.tenant, reason="subscription_expired")

        entry = wallet_service.grant(
            self.tenant, entry_type=EntryType.CYCLE_ALLOCATION,
            amount=1000, reference_id="ws09",
        )

        self.assertIsNotNone(entry)
        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.balance, 1000)
        self.assertTrue(wallet.is_frozen)

    def test_ws10_provenance_is_recorded(self):
        """WS-10: which cycle it belonged to, and who did it."""
        subscription = subscribe(self.tenant, self.plan)
        staff = User.objects.create(
            email="ws10@example.com", username="ws10", is_staff=True
        )

        entry = wallet_service.grant(
            self.tenant, entry_type=EntryType.ADMIN_ADJUSTMENT, amount=25,
            reference_id="ws10", subscription=subscription, performed_by=staff,
            notes="goodwill",
        )

        self.assertEqual(entry.subscription_id, subscription.pk)
        self.assertEqual(entry.performed_by_id, staff.pk)
        self.assertEqual(entry.notes, "goodwill")


class ConsumeTests(WalletServiceTestCase):
    """WS-11 to WS-19 — credits out, on the call path."""

    def test_ws11_a_ninety_second_call_costs_two_minutes(self):
        """WS-11: 10 credits a minute, ceil, so 90 seconds is 20 credits."""
        self.fund(allocated=1000)

        entry = wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_LOW_LATENCY,
            quantity=90, reference_id="call-ws11",
        )

        self.assertEqual(entry.amount, -20)
        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.balance, 980)

    def test_ws12_the_row_carries_its_own_rating(self):
        """WS-12: **this is what survives a re-pricing.**

        Without these four columns, next year's rate silently re-explains every
        historical charge and 'why was I charged 20?' stops having an answer.
        """
        self.fund(allocated=1000)

        entry = wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_LOW_LATENCY,
            quantity=90, reference_id="call-ws12",
        )

        self.assertEqual(entry.operation, Operation.CONVERSATION_LOW_LATENCY)
        self.assertEqual(entry.raw_quantity, 90)
        self.assertEqual(entry.units, 2)
        self.assertEqual(entry.unit_type, CreditRate.Unit.PER_MINUTE)
        self.assertEqual(entry.rate_applied, 10)
        self.assertIsNotNone(entry.rate_id)

    def test_ws13_a_replayed_call_is_charged_once(self):
        """WS-13: the runner is an external service that retries. ``provider_call_id``
        is already unique, so this defence is free."""
        self.fund(allocated=1000)

        first = wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_LOW_LATENCY,
            quantity=90, reference_id="call-replayed",
        )
        second = wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_LOW_LATENCY,
            quantity=90, reference_id="call-replayed",
        )

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(self.wallet.balance, 980)
        assert_invariant(self, self.tenant)

    def test_ws14_a_charge_that_empties_the_wallet_succeeds(self):
        """WS-14: 'exactly affordable' must not be off by one in either direction."""
        self.fund(allocated=20)

        entry = wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_LOW_LATENCY,
            quantity=90, reference_id="call-ws14",
        )

        self.assertIsNotNone(entry)
        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.balance, 0)

    def test_ws15_a_charge_one_credit_short_writes_nothing(self):
        """WS-15: a partial deduction would be worse than none — the tenant would be
        charged for a call and still see it recorded as unpaid."""
        self.fund(allocated=19)

        with self.assertLogs("wallet.services.wallet", level="WARNING"):
            entry = wallet_service.consume(
                self.tenant, operation=Operation.CONVERSATION_LOW_LATENCY,
                quantity=90, reference_id="call-ws15",
            )

        self.assertIsNone(entry)
        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.balance, 19)
        self.assertFalse(self.rows.filter(reference_id="call-ws15").exists())

    def test_ws16_a_zero_credit_charge_writes_no_row(self):
        """WS-16: a free tier is priced at 0, and the ledger's non-zero constraint
        would reject the row anyway. Skipping it is the correct behaviour, not a
        swallowed error."""
        self.fund(allocated=1000)

        entry = wallet_service.consume(
            self.tenant, operation=Operation.STATIC_REEL,
            quantity=600, reference_id="call-ws16",
        )

        self.assertIsNone(entry)
        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.balance, 1000)
        self.assertFalse(self.rows.filter(reference_id="call-ws16").exists())

    def test_ws17_lifetime_consumed_accumulates(self):
        """WS-17."""
        self.fund(allocated=1000)

        wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_LOW_LATENCY,
            quantity=90, reference_id="call-ws17a",
        )
        wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_HIGH_LATENCY,
            quantity=60, reference_id="call-ws17b",
        )

        self.assertEqual(self.wallet.lifetime_consumed, 25)

    def test_ws18_an_unpriced_operation_raises(self):
        """WS-18: raised loudly rather than charged at zero. A platform that
        silently gives away an unpriced operation discovers it as a revenue gap
        nobody can date."""
        bare_plan = make_plan("ws18-plan", with_rates=False)
        add_rates(bare_plan, {Operation.CONVERSATION_HIGH_LATENCY: 5})
        wallet_service.set_plan_context(self.tenant, bare_plan)
        self.fund(allocated=1000)

        with self.assertRaises(RateNotConfigured):
            wallet_service.consume(
                self.tenant, operation=Operation.CONVERSATION_LOW_LATENCY,
                quantity=90, reference_id="call-ws18",
            )

    def test_ws19_metadata_is_stored_on_the_row(self):
        """WS-19: structured context that does not deserve its own column."""
        self.fund(allocated=1000)

        entry = wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_LOW_LATENCY,
            quantity=90, reference_id="call-ws19",
            metadata={"direction": "outbound", "agent": "abc"},
        )

        self.assertEqual(entry.metadata["direction"], "outbound")


class BucketArithmeticTests(WalletServiceTestCase):
    """WS-20 to WS-25 — which bucket a charge comes out of, and how it is recorded."""

    def test_ws20_allocated_drains_first(self):
        """WS-20: **the credits the tenant paid cash for are the ones that survive
        to the next cycle.** Draining purchased first would quietly confiscate them
        at every renewal."""
        self.fund(allocated=980, purchased=50)

        wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_LOW_LATENCY,
            quantity=60, reference_id="call-ws20",
        )

        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.allocated_balance, 970)
        self.assertEqual(wallet.purchased_balance, 50)

    def test_ws21_a_charge_spanning_buckets_writes_two_rows(self):
        """WS-21: one row claiming ``bucket=allocated`` for credits that partly came
        from elsewhere would be a false statement in an audit trail whose whole job
        is explaining the balance."""
        self.fund(allocated=5, purchased=50)

        wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_HIGH_LATENCY,
            quantity=180, reference_id="call-ws21",
        )

        rows = self.rows.filter(entry_type=EntryType.USAGE_DEDUCTION)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(rows.get(bucket=Bucket.ALLOCATED).amount, -5)
        self.assertEqual(rows.get(bucket=Bucket.PURCHASED).amount, -10)

        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.allocated_balance, 0)
        self.assertEqual(wallet.purchased_balance, 40)

    def test_ws22_the_second_row_gets_a_derived_reference(self):
        """WS-22: the suffix keeps the unique constraint satisfied while staying
        obviously derived from the original event."""
        self.fund(allocated=5, purchased=50)

        wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_HIGH_LATENCY,
            quantity=180, reference_id="call-ws22",
        )

        references = set(
            self.rows.filter(entry_type=EntryType.USAGE_DEDUCTION)
            .values_list("reference_id", flat=True)
        )
        self.assertEqual(references, {"call-ws22", "call-ws22#purchased"})

    def test_ws23_replaying_a_split_charge_writes_nothing(self):
        """WS-23: the probe is on the primary reference only, and both rows share a
        transaction — so one check covers the pair."""
        self.fund(allocated=5, purchased=50)

        wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_HIGH_LATENCY,
            quantity=180, reference_id="call-ws23",
        )
        before = self.wallet.balance

        replay = wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_HIGH_LATENCY,
            quantity=180, reference_id="call-ws23",
        )

        self.assertIsNone(replay)
        self.assertEqual(self.wallet.balance, before)
        self.assertEqual(
            self.rows.filter(entry_type=EntryType.USAGE_DEDUCTION).count(), 2
        )
        assert_invariant(self, self.tenant)

    def test_ws24_each_bucket_reconciles_independently_after_a_split(self):
        """WS-24: the invariant holds per bucket, not merely in total. A charge
        booked against the wrong bucket keeps the total right and the buckets
        wrong, which is exactly the bug that hides."""
        self.fund(allocated=5, purchased=50)

        wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_HIGH_LATENCY,
            quantity=180, reference_id="call-ws24",
        )

        assert_invariant(self, self.tenant, msg="after a cross-bucket charge")

    def test_ws25_an_empty_allocated_bucket_produces_one_row(self):
        """WS-25: no phantom zero-credit allocated row alongside the real one."""
        self.fund(purchased=50)

        wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_LOW_LATENCY,
            quantity=90, reference_id="call-ws25",
        )

        rows = self.rows.filter(entry_type=EntryType.USAGE_DEDUCTION)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().bucket, Bucket.PURCHASED)

        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.purchased_balance, 30)


class RefundTests(WalletServiceTestCase):
    """WS-26 to WS-30 — giving a charge back."""

    def test_ws26_a_single_bucket_charge_is_restored_where_it_came_from(self):
        """WS-26."""
        self.fund(allocated=1000)
        wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_LOW_LATENCY,
            quantity=90, reference_id="call-ws26",
        )

        wallet_service.refund(self.tenant, reference_id="call-ws26")

        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.allocated_balance, 1000)
        self.assertEqual(wallet.purchased_balance, 0)

    def test_ws27_a_cross_bucket_charge_is_restored_exactly_as_drawn(self):
        """WS-27: **5 and 10, not 15 to one bucket.**

        Dumping the lot into allocated would quietly convert cash-bought credits
        into ones that evaporate at the next renewal.
        """
        self.fund(allocated=5, purchased=50)
        wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_HIGH_LATENCY,
            quantity=180, reference_id="call-ws27",
        )

        wallet_service.refund(self.tenant, reference_id="call-ws27")

        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.allocated_balance, 5)
        self.assertEqual(wallet.purchased_balance, 50)
        self.assertEqual(
            self.rows.filter(entry_type=EntryType.USAGE_REFUND).count(), 2
        )

    def test_ws28_a_replayed_refund_does_not_credit_twice(self):
        """WS-28."""
        self.fund(allocated=1000)
        wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_LOW_LATENCY,
            quantity=90, reference_id="call-ws28",
        )
        wallet_service.refund(self.tenant, reference_id="call-ws28")
        before = self.wallet.balance

        again = wallet_service.refund(self.tenant, reference_id="call-ws28")

        self.assertIsNone(again)
        self.assertEqual(self.wallet.balance, before)
        assert_invariant(self, self.tenant)

    def test_ws29_refunding_an_unknown_reference_is_a_logged_no_op(self):
        """WS-29: there is nothing to reverse, and inventing a credit would be
        creating money from a typo."""
        self.fund(allocated=1000)

        with self.assertLogs("wallet.services.wallet", level="WARNING") as logs:
            result = wallet_service.refund(self.tenant, reference_id="never-charged")

        self.assertIsNone(result)
        self.assertIn("never-charged", "".join(logs.output))
        self.assertEqual(self.wallet.balance, 1000)

    def test_ws30_lifetime_consumed_never_goes_negative(self):
        """WS-30: a counter that can go below zero produces usage reports nobody
        can explain."""
        self.fund(allocated=1000)
        wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_LOW_LATENCY,
            quantity=90, reference_id="call-ws30",
        )
        # Simulate a counter that has drifted below the amount being returned.
        Wallet.objects.filter(tenant=self.tenant).update(lifetime_consumed=5)

        wallet_service.refund(self.tenant, reference_id="call-ws30")

        self.assertEqual(self.wallet.lifetime_consumed, 0)
        assert_invariant(self, self.tenant)


class ForfeitTests(WalletServiceTestCase):
    """WS-31 to WS-36 — burning credits with nothing given back."""

    def test_ws31_forfeiting_a_specific_amount(self):
        """WS-31."""
        self.fund(allocated=1000, purchased=200)

        wallet_service.forfeit(
            self.tenant, entry_type=EntryType.ROLLOVER_CAP_FORFEIT,
            amount=300, reference_id="ws31",
        )

        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.allocated_balance, 700)
        self.assertEqual(wallet.purchased_balance, 200)

    def test_ws32_forfeiting_the_whole_bucket(self):
        """WS-32: what expiry does — and it must leave the purchased bucket alone,
        because those credits were paid for directly."""
        self.fund(allocated=1000, purchased=200)

        wallet_service.forfeit(
            self.tenant, entry_type=EntryType.EXPIRY_FORFEIT,
            amount=None, reference_id="ws32",
        )

        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.allocated_balance, 0)
        self.assertEqual(wallet.purchased_balance, 200)
        self.assertEqual(wallet.balance, 200)

    def test_ws33_forfeiting_more_than_is_held_is_clamped(self):
        """WS-33: never a negative balance, whatever the caller asks for."""
        self.fund(allocated=100)

        entry = wallet_service.forfeit(
            self.tenant, entry_type=EntryType.ROLLOVER_FORFEIT,
            amount=999999, reference_id="ws33",
        )

        self.assertEqual(entry.amount, -100)
        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.allocated_balance, 0)

    def test_ws34_forfeiting_an_empty_bucket_writes_nothing(self):
        """WS-34: a zero-amount row is rejected by the ledger constraint, and
        would be noise in the audit trail even if it were not."""
        self.fund(purchased=200)

        entry = wallet_service.forfeit(
            self.tenant, entry_type=EntryType.EXPIRY_FORFEIT,
            amount=None, reference_id="ws34",
        )

        self.assertIsNone(entry)
        self.assertFalse(self.rows.filter(reference_id="ws34").exists())
        assert_invariant(self, self.tenant)

    def test_ws35_a_forfeit_is_not_consumption(self):
        """WS-35: **credits were removed, but nothing was used.**

        Counting a forfeit as consumption overstates every usage report built on
        that figure, including the ones shown to the tenant.
        """
        self.fund(allocated=1000)
        before = self.wallet.lifetime_consumed

        wallet_service.forfeit(
            self.tenant, entry_type=EntryType.EXPIRY_FORFEIT,
            amount=None, reference_id="ws35",
        )

        self.assertEqual(self.wallet.lifetime_consumed, before)

    def test_ws36_a_replayed_forfeit_is_a_no_op(self):
        """WS-36: expiry can be retried by a beat run that overlaps its predecessor."""
        self.fund(allocated=1000)
        wallet_service.forfeit(
            self.tenant, entry_type=EntryType.EXPIRY_FORFEIT,
            amount=500, reference_id="ws36",
        )
        before = self.wallet.balance

        again = wallet_service.forfeit(
            self.tenant, entry_type=EntryType.EXPIRY_FORFEIT,
            amount=500, reference_id="ws36",
        )

        self.assertIsNone(again)
        self.assertEqual(self.wallet.balance, before)
        assert_invariant(self, self.tenant)


class AdjustTests(WalletServiceTestCase):
    """WS-37 to WS-41 — the sanctioned way for staff to move credits."""

    def setUp(self):
        super().setUp()
        self.staff = User.objects.create(
            email="adjust@example.com", username="adjust-staff", is_staff=True
        )

    def test_ws37_a_positive_adjustment_lands_in_the_purchased_bucket(self):
        """WS-37: **goodwill credits should survive the next renewal.**

        Landing them in allocated would make them evaporate at a cycle boundary and
        generate a second complaint about the first complaint.
        """
        self.fund(allocated=100)

        wallet_service.adjust(
            self.tenant, amount=250, reference_id="ticket-4471",
            performed_by=self.staff, notes="apology for the outage",
        )

        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.purchased_balance, 250)
        self.assertEqual(wallet.allocated_balance, 100)

    def test_ws38_a_negative_adjustment_drains_allocated_first(self):
        """WS-38: same ordering as a real charge, so a correction cannot quietly
        reshape which bucket the tenant's remaining credits sit in."""
        self.fund(allocated=100, purchased=200)

        wallet_service.adjust(
            self.tenant, amount=-150, reference_id="ticket-4472",
            performed_by=self.staff,
        )

        wallet = assert_invariant(self, self.tenant)
        self.assertEqual(wallet.allocated_balance, 0)
        self.assertEqual(wallet.purchased_balance, 150)

    def test_ws39_an_adjustment_of_zero_is_refused(self):
        """WS-39."""
        with self.assertRaises(ValueError):
            wallet_service.adjust(
                self.tenant, amount=0, reference_id="ticket-4473"
            )

    def test_ws40_the_staff_user_is_recorded(self):
        """WS-40: an unattributed adjustment is indistinguishable from a bug."""
        self.fund(allocated=100)

        entry = wallet_service.adjust(
            self.tenant, amount=50, reference_id="ticket-4474",
            performed_by=self.staff, notes="manual credit",
        )

        self.assertEqual(entry.performed_by_id, self.staff.pk)
        self.assertEqual(entry.entry_type, EntryType.ADMIN_ADJUSTMENT)
        self.assertEqual(entry.notes, "manual credit")

    def test_ws41_a_replayed_adjustment_is_a_no_op(self):
        """WS-41: a double-clicked admin action must not pay out twice."""
        self.fund(allocated=100)
        wallet_service.adjust(
            self.tenant, amount=50, reference_id="ticket-4475",
            performed_by=self.staff,
        )
        before = self.wallet.balance

        again = wallet_service.adjust(
            self.tenant, amount=50, reference_id="ticket-4475",
            performed_by=self.staff,
        )

        self.assertIsNone(again)
        self.assertEqual(self.wallet.balance, before)
        assert_invariant(self, self.tenant)


class GuardAndStateTests(WalletServiceTestCase):
    """WS-42 to WS-51 and WS-53 to WS-55 — authorising, freezing, reading."""

    def enforce(self, credits_enforced=True):
        settings_row = TenantSettings.for_tenant(self.tenant.id)
        settings_row.credits_enforced = credits_enforced
        settings_row.save(update_fields=["credits_enforced"])
        return settings_row

    # --- assert_can_consume -------------------------------------------------

    def test_ws42_a_funded_wallet_is_authorised(self):
        """WS-42."""
        self.fund(allocated=1000)
        self.enforce()

        wallet_service.assert_can_consume(
            self.tenant, operation=Operation.CONVERSATION_LOW_LATENCY, quantity=90
        )

    def test_ws43_a_shortfall_only_logs_while_enforcement_is_off(self):
        """WS-43: **metering before blocking.**

        A full cycle of real traffic has to prove the rates are right before a wrong
        number is allowed to stop a paying customer's campaign.
        """
        self.fund(allocated=5)

        with self.assertLogs("wallet.services.wallet", level="WARNING") as logs:
            wallet_service.assert_can_consume(
                self.tenant, operation=Operation.CONVERSATION_LOW_LATENCY,
                quantity=90,
            )

        self.assertIn("would refuse", "".join(logs.output))

    def test_ws44_a_shortfall_raises_once_enforcement_is_on(self):
        """WS-44: and the exception carries both numbers, because the view turns
        them into something the tenant can act on."""
        self.fund(allocated=5)
        self.enforce()

        with self.assertRaises(InsufficientCredits) as caught:
            wallet_service.assert_can_consume(
                self.tenant, operation=Operation.CONVERSATION_LOW_LATENCY,
                quantity=90,
            )

        self.assertEqual(caught.exception.required, 20)
        self.assertEqual(caught.exception.available, 5)

    def test_ws45_a_frozen_wallet_only_logs_while_enforcement_is_off(self):
        """WS-45."""
        self.fund(allocated=1000)
        wallet_service.freeze(self.tenant, reason="subscription_expired")

        with self.assertLogs("wallet.services.wallet", level="WARNING") as logs:
            wallet_service.assert_can_consume(
                self.tenant, operation=Operation.CONVERSATION_LOW_LATENCY,
                quantity=90,
            )

        self.assertIn("frozen", "".join(logs.output))

    def test_ws46_a_frozen_wallet_raises_its_own_exception(self):
        """WS-46: distinct from ``InsufficientCredits`` because the remedy differs —
        a frozen wallet needs a renewal, not a top-up."""
        self.fund(allocated=1000)
        wallet_service.freeze(self.tenant, reason="subscription_expired")
        self.enforce()

        with self.assertRaises(WalletFrozen):
            wallet_service.assert_can_consume(
                self.tenant, operation=Operation.CONVERSATION_LOW_LATENCY,
                quantity=90,
            )

    def test_ws47_the_authorisation_path_does_not_write(self):
        """WS-47: ``TenantSettings.for_tenant()`` would create a row, and this runs
        before every call. A read path that writes is a write path."""
        self.fund(allocated=1000)
        # all_objects, because the default manager refuses bulk deletes.
        TenantSettings.all_objects.filter(tenant=self.tenant).delete()
        self.assertFalse(TenantSettings.all_objects.filter(tenant=self.tenant).exists())

        wallet_service.assert_can_consume(
            self.tenant, operation=Operation.CONVERSATION_LOW_LATENCY, quantity=90
        )

        self.assertFalse(
            TenantSettings.all_objects.filter(tenant=self.tenant).exists(),
            "authorising a call created a TenantSettings row",
        )

    # --- freeze / unfreeze --------------------------------------------------

    def test_ws48_freezing_records_why_and_when(self):
        """WS-48: 'frozen' with no reason is an unanswerable support ticket."""
        wallet = wallet_service.freeze(self.tenant, reason="subscription_expired")

        self.assertTrue(wallet.is_frozen)
        self.assertEqual(wallet.frozen_reason, "subscription_expired")
        self.assertIsNotNone(wallet.frozen_at)

    def test_ws49_refreezing_preserves_the_original_reason(self):
        """WS-49: the first reason is the true one. Overwriting it with a later,
        vaguer one loses the only record of why spending stopped."""
        first = wallet_service.freeze(self.tenant, reason="subscription_expired")
        original_at = first.frozen_at

        second = wallet_service.freeze(self.tenant, reason="fraud_review")

        self.assertEqual(second.frozen_reason, "subscription_expired")
        self.assertEqual(second.frozen_at, original_at)

    def test_ws50_unfreezing_clears_the_reason_and_the_timestamp(self):
        """WS-50: a stale reason on an active wallet reads as a live problem."""
        wallet_service.freeze(self.tenant, reason="subscription_expired")

        wallet = wallet_service.unfreeze(self.tenant)

        self.assertFalse(wallet.is_frozen)
        self.assertIsNone(wallet.frozen_reason)
        self.assertIsNone(wallet.frozen_at)

    def test_ws51_setting_plan_context_stamps_rates_and_the_alert_threshold(self):
        """WS-51: both come from the plan, and both are part of what the tenant
        bought. A stamp without the threshold leaves alerting on the old plan's
        numbers."""
        richer = make_plan(
            "ws51-plan", rates={Operation.CONVERSATION_LOW_LATENCY: 3},
            low_balance_threshold=777,
        )

        wallet = wallet_service.set_plan_context(self.tenant, richer)

        self.assertEqual(wallet.current_plan_id, richer.pk)
        self.assertEqual(wallet.low_balance_threshold, 777)
        self.assertEqual(
            wallet.rate_snapshot["operations"][
                Operation.CONVERSATION_LOW_LATENCY
            ]["credits_per_unit"],
            3,
        )

    # --- reading ------------------------------------------------------------

    def test_ws53_the_invariant_survives_a_long_mixed_sequence(self):
        """WS-53: **the fuzz case.**

        Fifty grants, charges, refunds, forfeits and adjustments in a seeded order.
        Individually every one of them is covered above; what this catches is a
        combination nobody thought to write a case for.
        """
        rng = random.Random(20260825)
        self.fund(allocated=5000, purchased=5000)
        charged = []

        for step in range(50):
            action = rng.choice(
                ["grant", "consume", "consume", "refund", "forfeit", "adjust"]
            )
            reference = f"fuzz-{step}"

            if action == "grant":
                wallet_service.grant(
                    self.tenant, entry_type=EntryType.MANUAL_TOP_UP,
                    amount=rng.randint(1, 200), bucket=Bucket.PURCHASED,
                    reference_id=reference,
                )
            elif action == "consume":
                entry = wallet_service.consume(
                    self.tenant,
                    operation=rng.choice([
                        Operation.CONVERSATION_HIGH_LATENCY,
                        Operation.CONVERSATION_LOW_LATENCY,
                    ]),
                    quantity=rng.randint(1, 600),
                    reference_id=reference,
                )
                if entry is not None:
                    charged.append(reference)
            elif action == "refund" and charged:
                wallet_service.refund(
                    self.tenant, reference_id=charged.pop(rng.randrange(len(charged)))
                )
            elif action == "forfeit":
                wallet_service.forfeit(
                    self.tenant, entry_type=EntryType.ROLLOVER_CAP_FORFEIT,
                    amount=rng.randint(1, 100), reference_id=reference,
                )
            elif action == "adjust":
                amount = rng.choice([-1, 1]) * rng.randint(1, 150)
                wallet_service.adjust(
                    self.tenant, amount=amount, reference_id=reference
                )

            assert_invariant(self, self.tenant, msg=f"step {step} ({action})")

        wallet = self.wallet
        self.assertGreaterEqual(wallet.balance, 0)

    def test_ws54_balance_for_returns_everything_a_dashboard_needs(self):
        """WS-54: rendered straight into an API response, so a missing key is a
        broken screen rather than a caught error."""
        self.fund(allocated=1000, purchased=500)

        summary = wallet_service.balance_for(self.tenant)

        self.assertEqual(summary["balance"], 1500)
        self.assertEqual(summary["allocated"], 1000)
        self.assertEqual(summary["purchased"], 500)
        self.assertFalse(summary["is_frozen"])
        self.assertFalse(summary["is_low"])
        self.assertEqual(summary["low_balance_threshold"], 100)
        self.assertEqual(summary["plan_code"], self.plan.code)

    def test_ws55_quoting_costs_nothing_and_writes_nothing(self):
        """WS-55: a price check is not a purchase. Anything else would let a
        pricing widget drain a wallet."""
        self.fund(allocated=1000)
        before = self.rows.count()

        result = wallet_service.quote(
            self.tenant, operation=Operation.CONVERSATION_LOW_LATENCY, quantity=90
        )

        self.assertEqual(result.credits, 20)
        self.assertEqual(self.rows.count(), before)
        self.assertEqual(self.wallet.balance, 1000)


class ConcurrentConsumeTests(TransactionTestCase):
    """WS-52 — two real connections racing for the last credit.

    ``TransactionTestCase`` rather than ``TestCase`` on purpose: the lock this proves
    is ``SELECT FOR UPDATE``, and a second lock taken *inside the same transaction*
    does not block. Testing this on one connection would prove nothing at all.
    """

    def test_ws52_only_one_of_two_racing_charges_succeeds(self):
        """WS-52: **without the row lock both threads read a sufficient balance and
        both spend it**, and the wallet ends up owing credits it never sold."""
        ctx = make_tenant("race")
        tenant = ctx.tenant
        plan = make_plan(
            "race-plan", rates={Operation.CONVERSATION_HIGH_LATENCY: 1}
        )
        wallet_service.set_plan_context(tenant, plan)
        wallet_service.grant(
            tenant, entry_type=EntryType.CYCLE_ALLOCATION,
            amount=1, reference_id="race-seed",
        )

        started = threading.Barrier(2, timeout=10)
        outcomes = {}

        def charge(name):
            try:
                started.wait()
                outcomes[name] = wallet_service.consume(
                    tenant,
                    operation=Operation.CONVERSATION_HIGH_LATENCY,
                    quantity=60,
                    reference_id=f"race-{name}",
                )
            except Exception as exc:  # surfaced as a failure below, not swallowed
                outcomes[name] = exc
            finally:
                connection.close()

        threads = [
            threading.Thread(target=charge, args=(name,)) for name in ("a", "b")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive(), "a racing charge never finished")

        for name, outcome in outcomes.items():
            self.assertNotIsInstance(
                outcome, Exception, f"thread {name} raised: {outcome}"
            )

        succeeded = [entry for entry in outcomes.values() if entry is not None]
        self.assertEqual(
            len(succeeded), 1,
            "exactly one of two racing charges must land on a one-credit wallet",
        )

        wallet = Wallet.objects.get(tenant=tenant)
        self.assertEqual(wallet.balance, 0)
        self.assertEqual(
            CreditLedger.all_objects.filter(
                tenant=tenant, entry_type=EntryType.USAGE_DEDUCTION
            ).count(),
            1,
        )
        assert_invariant(self, tenant, msg="after a concurrent charge")
