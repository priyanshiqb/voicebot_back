"""Section 3 of the billing test plan — WR-01 to WR-26.

Two directions that deliberately touch different tables:

* ``build_snapshot`` / ``stamp`` read the ``CreditRate`` catalogue, a handful of times
  per tenant per year.
* ``resolve_rate`` / ``rate_quantity`` read the wallet's own stamp, on every call.

The arithmetic cases below are the ones a tenant argues about on an invoice, so they
are asserted as whole numbers with the working shown, not as "roughly right".
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from onboard.tests.billing_factories import add_rates, make_plan, make_tenant
from wallet.models import CreditLedger, CreditRate, Wallet
from wallet.services import rating
from wallet.services import wallet as wallet_service
from wallet.services.exceptions import RateNotConfigured

Operation = CreditRate.Operation
Unit = CreditRate.Unit
Rounding = CreditRate.Rounding


def resolved(**overrides):
    """A ``ResolvedRate`` with the common shape: 5 credits a minute, ceil, minimum 1."""
    fields = {
        "operation": Operation.CONVERSATION_HIGH_LATENCY,
        "unit": Unit.PER_MINUTE,
        "credits_per_unit": 5,
        "rounding": Rounding.CEIL,
        "minimum_units": 1,
        "rate_id": "rate-under-test",
    }
    fields.update(overrides)
    return rating.ResolvedRate(**fields)


class RateQuantityTests(TestCase):
    """WR-01 to WR-16 — the arithmetic. No database involved."""

    def test_wr01_ninety_seconds_bills_two_minutes(self):
        """WR-01: the canonical case. ceil(90/60) = 2 units at 5 = 10 credits."""
        result = rating.rate_quantity(resolved(), 90)

        self.assertEqual(result.units, 2)
        self.assertEqual(result.credits, 10)

    def test_wr02_exactly_one_minute_bills_one(self):
        """WR-02."""
        result = rating.rate_quantity(resolved(), 60)

        self.assertEqual(result.units, 1)
        self.assertEqual(result.credits, 5)

    def test_wr03_one_second_over_bills_the_next_minute(self):
        """WR-03: the ceil boundary, and standard telecom billing. A tenant who
        disputes this is disputing the rounding mode, not a bug."""
        result = rating.rate_quantity(resolved(), 61)

        self.assertEqual(result.units, 2)
        self.assertEqual(result.credits, 10)

    def test_wr04_an_exact_multiple_does_not_round_up(self):
        """WR-04: ceil must not add a phantom minute to a call that landed exactly
        on the boundary."""
        result = rating.rate_quantity(resolved(), 180)

        self.assertEqual(result.units, 3)
        self.assertEqual(result.credits, 15)

    def test_wr05_a_one_second_call_still_bills_the_minimum(self):
        """WR-05: minimum_units = 1 means the shortest call is a full minute."""
        result = rating.rate_quantity(resolved(), 1)

        self.assertEqual(result.units, 1)
        self.assertEqual(result.credits, 5)

    def test_wr06_zero_is_rated_without_error(self):
        """WR-06: rating zero is arithmetic, not a decision. Whether a zero-duration
        call is charged at all is the policy layer's call — see WP-04."""
        result = rating.rate_quantity(resolved(), 0)

        self.assertEqual(result.raw_quantity, 0)
        self.assertEqual(result.units, 1)
        self.assertEqual(result.credits, 5)

    # --- rounding modes -----------------------------------------------------

    def test_wr07_floor_discards_the_partial_minute(self):
        """WR-07."""
        result = rating.rate_quantity(resolved(rounding=Rounding.FLOOR), 90)

        self.assertEqual(result.units, 1)
        self.assertEqual(result.credits, 5)

    def test_wr08_nearest_rounds_a_half_up(self):
        """WR-08: 90/60 is exactly 1.5, and half-up is the convention money uses."""
        result = rating.rate_quantity(resolved(rounding=Rounding.NEAREST), 90)

        self.assertEqual(result.units, 2)

    def test_wr09_nearest_rounds_below_a_half_down(self):
        """WR-09: 89/60 is 1.483..., so it must not become 2."""
        result = rating.rate_quantity(resolved(rounding=Rounding.NEAREST), 89)

        self.assertEqual(result.units, 1)

    # --- minimums and unit types -------------------------------------------

    def test_wr10_the_minimum_wins_over_the_computed_units(self):
        """WR-10: a two-minute minimum bills two minutes for a 30-second call."""
        result = rating.rate_quantity(resolved(minimum_units=2), 30)

        self.assertEqual(result.units, 2)
        self.assertEqual(result.credits, 10)

    def test_wr11_per_event_ignores_duration_entirely(self):
        """WR-11: a flat price per call. Two very different durations, same charge."""
        short = rating.rate_quantity(resolved(unit=Unit.PER_EVENT), 5)
        long = rating.rate_quantity(resolved(unit=Unit.PER_EVENT), 3600)

        self.assertEqual(short.units, 1)
        self.assertEqual(long.units, 1)
        self.assertEqual(short.credits, long.credits)
        self.assertEqual(short.credits, 5)

    def test_wr12_per_event_still_respects_a_minimum(self):
        """WR-12: an odd combination, but the minimum is not silently ignored."""
        result = rating.rate_quantity(
            resolved(unit=Unit.PER_EVENT, minimum_units=3), 5
        )

        self.assertEqual(result.units, 3)
        self.assertEqual(result.credits, 15)

    def test_wr13_per_second_bills_every_second(self):
        """WR-13: divisor 1, so units equal the raw quantity."""
        result = rating.rate_quantity(
            resolved(unit=Unit.PER_SECOND, credits_per_unit=1), 90
        )

        self.assertEqual(result.units, 90)
        self.assertEqual(result.credits, 90)

    def test_wr14_a_negative_quantity_is_clamped_to_zero(self):
        """WR-14: a provider reporting a negative duration must not produce a
        *credit* on a deduction path."""
        result = rating.rate_quantity(resolved(), -600)

        self.assertEqual(result.raw_quantity, 0)
        self.assertEqual(result.units, 1)
        self.assertEqual(result.credits, 5)

    def test_wr15_a_zero_price_is_a_free_tier_not_an_error(self):
        """WR-15: free plans carry a complete rate card priced at 0. A missing row
        is the misconfiguration; a zero row is a deliberate offer."""
        result = rating.rate_quantity(resolved(credits_per_unit=0), 3600)

        self.assertEqual(result.units, 60)
        self.assertEqual(result.credits, 0)

    def test_wr16_the_result_carries_everything_the_ledger_snapshots(self):
        """WR-16: this is what answers 'why was I charged 10 credits?' a year later,
        after the plan has been re-priced twice."""
        result = rating.rate_quantity(resolved(), 90)

        self.assertEqual(result.raw_quantity, 90)
        self.assertEqual(result.units, 2)
        self.assertEqual(result.unit_type, Unit.PER_MINUTE)
        self.assertEqual(result.rate_applied, 5)
        self.assertEqual(result.operation, Operation.CONVERSATION_HIGH_LATENCY)
        self.assertEqual(result.rate_id, "rate-under-test")
        self.assertEqual(result.credits, 10)


class ResolveRateTests(TestCase):
    """WR-17 to WR-21 — where a price comes from, in order of preference."""

    def setUp(self):
        self.tenant = make_tenant("resolve-rate").tenant
        # A deliberately incomplete card: two tiers priced, the third priced
        # nowhere, so the fall-through in WR-20 has something to fall through to.
        self.plan = make_plan("resolve-rate-plan", with_rates=False)
        add_rates(self.plan, {
            Operation.CONVERSATION_HIGH_LATENCY: 8,
            Operation.STATIC_REEL: 3,
        })
        self.wallet = Wallet.objects.create(tenant=self.tenant)

    def test_wr17_a_stamped_wallet_resolves_without_touching_the_database(self):
        """WR-17: **the hot path.** Every call rates through here, so a query
        creeping in is a platform-wide regression, not a slow test."""
        rating.stamp(self.wallet, self.plan)
        self.wallet.refresh_from_db()

        with self.assertNumQueries(0):
            rate = rating.resolve_rate(
                self.wallet, Operation.CONVERSATION_HIGH_LATENCY
            )

        self.assertEqual(rate.source, "snapshot")
        self.assertEqual(rate.credits_per_unit, 8)

    def test_wr18_an_unstamped_wallet_falls_back_to_the_live_plan_and_warns(self):
        """WR-18: a wallet with a plan but no stamp means some subscription path
        skipped ``set_plan_context``. Rating still works, loudly."""
        self.wallet.current_plan = self.plan
        self.wallet.save(update_fields=["current_plan"])

        with self.assertLogs("wallet.services.rating", level="WARNING") as logs:
            rate = rating.resolve_rate(
                self.wallet, Operation.CONVERSATION_HIGH_LATENCY
            )

        self.assertEqual(rate.source, "live_plan")
        self.assertEqual(rate.credits_per_unit, 8)
        self.assertIn("set_plan_context", "".join(logs.output))

    def test_wr19_no_stamp_and_no_plan_raises(self):
        """WR-19: there is no platform-wide default rate to fall through to, and
        inventing one is how a tenant gets billed at a price nobody agreed."""
        with self.assertRaises(RateNotConfigured):
            rating.resolve_rate(self.wallet, Operation.CONVERSATION_HIGH_LATENCY)

    def test_wr20_a_stamp_missing_the_operation_falls_back_then_raises(self):
        """WR-20: a partial stamp is worse than none, because the wallet looks
        configured. Fall back for what the plan does price; raise for what it
        does not."""
        rating.stamp(self.wallet, self.plan)
        self.wallet.refresh_from_db()

        # STATIC_REEL is priced on the plan but absent from this stamp.
        self.wallet.rate_snapshot["operations"].pop(Operation.STATIC_REEL, None)

        rate = rating.resolve_rate(self.wallet, Operation.STATIC_REEL)
        self.assertEqual(rate.source, "live_plan")
        self.assertEqual(rate.credits_per_unit, 3)

        # Nothing prices this one anywhere — not the stamp, not the plan.
        with self.assertRaises(RateNotConfigured):
            rating.resolve_rate(self.wallet, Operation.CONVERSATION_LOW_LATENCY)

    def test_wr21_a_stamp_missing_optional_keys_uses_safe_defaults(self):
        """WR-21: an older stamp written before a key existed must keep rating.
        Ceil and 1 are the field defaults, so the wallet behaves as it did."""
        self.wallet.rate_snapshot = {
            "plan_id": str(self.plan.pk),
            "plan_code": self.plan.code,
            "operations": {
                Operation.CONVERSATION_HIGH_LATENCY: {
                    "unit": Unit.PER_MINUTE,
                    "credits_per_unit": 8,
                }
            },
        }
        self.wallet.save(update_fields=["rate_snapshot"])

        rate = rating.resolve_rate(self.wallet, Operation.CONVERSATION_HIGH_LATENCY)

        self.assertEqual(rate.rounding, Rounding.CEIL)
        self.assertEqual(rate.minimum_units, 1)
        self.assertIsNone(rate.rate_id)


class SnapshotTests(TestCase):
    """WR-22 to WR-26 — copying the catalogue onto a wallet."""

    def setUp(self):
        self.ctx = make_tenant("snapshot")
        self.tenant = self.ctx.tenant
        self.plan = make_plan("snapshot-plan")
        self.wallet = Wallet.objects.create(tenant=self.tenant)
        self.now = timezone.now()

    def test_wr22_a_snapshot_covers_every_current_rate(self):
        """WR-22: three tiers in, three tiers out, plus the provenance a support
        conversation needs."""
        snapshot = rating.build_snapshot(self.plan)

        self.assertEqual(snapshot["plan_id"], str(self.plan.pk))
        self.assertEqual(snapshot["plan_code"], self.plan.code)
        self.assertIn("stamped_at", snapshot)
        self.assertEqual(
            set(snapshot["operations"]),
            {
                Operation.STATIC_REEL,
                Operation.CONVERSATION_HIGH_LATENCY,
                Operation.CONVERSATION_LOW_LATENCY,
            },
        )
        self.assertEqual(
            snapshot["operations"][Operation.CONVERSATION_LOW_LATENCY][
                "credits_per_unit"
            ],
            12,
        )
        # The sort key used to pick a winner must not leak into stored JSON.
        for entry in snapshot["operations"].values():
            self.assertNotIn("_effective_from", entry)

    def test_wr23_superseded_rates_are_excluded(self):
        """WR-23: history stays in the catalogue and out of the stamp. A closed
        row must never be what a wallet is charged at."""
        current = CreditRate.objects.get(
            plan=self.plan, operation=Operation.STATIC_REEL
        )
        current.effective_to = self.now - timedelta(days=1)
        current.save(update_fields=["effective_to"])

        snapshot = rating.build_snapshot(self.plan)

        self.assertNotIn(Operation.STATIC_REEL, snapshot["operations"])
        self.assertIn(Operation.CONVERSATION_HIGH_LATENCY, snapshot["operations"])

    def test_wr24_when_two_rates_are_live_the_newest_wins(self):
        """WR-24: defensive. The unique constraint stops two *open-ended* rows, but
        a row closing in the future is legitimately live alongside one that is
        open — and a coin flip at stamp time would be indefensible."""
        CreditRate.objects.create(
            plan=self.plan,
            operation=Operation.STATIC_REEL,
            unit=Unit.PER_MINUTE,
            credits_per_unit=99,
            effective_from=self.now - timedelta(hours=1),
            effective_to=self.now + timedelta(days=30),
        )

        snapshot = rating.build_snapshot(self.plan)

        self.assertEqual(
            snapshot["operations"][Operation.STATIC_REEL]["credits_per_unit"], 99
        )

    def test_wr25_stamping_writes_all_three_columns(self):
        """WR-25: the snapshot, when it was taken, and which plan it came from.
        The third is display-only, but a stamp without it cannot be explained."""
        rating.stamp(self.wallet, self.plan)
        self.wallet.refresh_from_db()

        self.assertIsNotNone(self.wallet.rate_snapshot)
        self.assertIsNotNone(self.wallet.rate_snapshot_at)
        self.assertEqual(self.wallet.current_plan_id, self.plan.pk)
        self.assertEqual(self.wallet.rate_snapshot["plan_code"], self.plan.code)

    def test_wr26_restamping_reprices_the_next_call_and_leaves_history_alone(self):
        """WR-26: **the reason the ledger snapshots its own rating.**

        A plan change re-prices what happens next. It must not re-explain what
        already happened — a charge raised at 8 credits a minute stays a charge
        raised at 8 credits a minute, whatever the tenant pays today.
        """
        wallet_service.set_plan_context(self.tenant, self.plan)
        wallet_service.grant(
            self.tenant, entry_type=CreditLedger.EntryType.CYCLE_ALLOCATION,
            amount=1000, reference_id="grant-wr26",
        )
        first = wallet_service.consume(
            self.tenant,
            operation=Operation.CONVERSATION_HIGH_LATENCY,
            quantity=60,
            reference_id="call-wr26-a",
        )
        self.assertEqual(first.rate_applied, 8)
        self.assertEqual(abs(first.amount), 8)

        cheaper = make_plan("snapshot-plan-cheaper", rates={
            Operation.CONVERSATION_HIGH_LATENCY: 3,
        })
        wallet_service.set_plan_context(self.tenant, cheaper)

        second = wallet_service.consume(
            self.tenant,
            operation=Operation.CONVERSATION_HIGH_LATENCY,
            quantity=60,
            reference_id="call-wr26-b",
        )

        self.assertEqual(second.rate_applied, 3)
        self.assertEqual(abs(second.amount), 3)

        first.refresh_from_db()
        self.assertEqual(first.rate_applied, 8)
        self.assertEqual(abs(first.amount), 8)
