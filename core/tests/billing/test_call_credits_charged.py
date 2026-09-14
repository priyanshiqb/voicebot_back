"""Phase 0 of the campaign report plan: ``AgentCallLog.credits_charged``.

The field exists so nothing has to join the ledger to a call on a string. That join
silently undercounts: a charge drawn from both wallet buckets writes two ledger rows,
and only the first carries the bare call id. Most of these tests are that one bug seen
from different sides — the charge path, a free call, the failures, the backfill.

Null and zero mean different things, and several tests exist to hold that line: null is
"never charged" (and should stay visible as unrated), zero is "rated, and free".
"""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from rest_framework import status

from core.constants.service_tier import ServiceTier
from core.models.agent_call_log import AgentCallLog
from core.tests.billing.test_credit_consumption import RATES, ConsumptionTestCase
from onboard.services import subscriptions as subscription_service
from onboard.tests.billing_factories import make_agent, make_plan, make_tenant
from wallet.models import CreditLedger, CreditRate
from wallet.services import wallet as wallet_service

Bucket = CreditLedger.Bucket
EntryType = CreditLedger.EntryType
Operation = CreditRate.Operation

# 201 minutes on the 5-credit standard tier is 1,005 credits against a 1,000-credit
# allocation, so the last 5 have to come from the purchased bucket.
SPLIT_CALL_SECONDS = 201 * 60
SPLIT_CALL_CREDITS = 1005


class CreditsChargedTestCase(ConsumptionTestCase):
    def log(self, provider_call_id):
        return AgentCallLog.objects.get(provider_call_id=provider_call_id)

    def top_up(self, amount=500):
        """Purchased credits, so a large enough call has to span both buckets."""
        wallet_service.grant(
            self.tenant, entry_type=EntryType.MANUAL_TOP_UP, amount=amount,
            bucket=Bucket.PURCHASED, reference_id="credits-charged-top-up",
        )


class ChargePathTests(CreditsChargedTestCase):
    def test_a_charged_call_records_its_credits(self):
        """90 seconds on the 5-credit tier, ceil, is 10 credits (as CC-02)."""
        self.post_call(provider_call_id="call_cr01", duration_seconds=90)

        self.assertEqual(self.log("call_cr01").credits_charged, 10)

    def test_a_charge_split_across_buckets_records_the_full_total(self):
        """**The regression.** The first ledger row holds only the allocated share, so
        reading the total off it — or joining on the bare call id — gets 1,000."""
        self.top_up()

        self.post_call(
            provider_call_id="call_cr02", duration_seconds=SPLIT_CALL_SECONDS
        )

        rows = self.deductions()
        self.assertEqual(rows.count(), 2)
        self.assertEqual(rows.get(reference_id="call_cr02").amount, -1000)
        self.assertEqual(self.log("call_cr02").credits_charged, SPLIT_CALL_CREDITS)

    def test_a_zero_duration_call_stays_null(self):
        self.post_call(provider_call_id="call_cr03", duration_seconds=0)

        self.assertIsNone(self.log("call_cr03").credits_charged)

    def test_a_short_wallet_stays_null_not_zero(self):
        """Nothing was charged, so null. A 0 here would hide the call from anyone
        looking for calls that were never billed."""
        broke = make_tenant("credits-broke")
        plan = make_plan("credits-broke-plan", rates=RATES)
        subscription_service.activate(broke, plan, credit_grant=2)
        agent = make_agent(
            broke.tenant, service_tier=ServiceTier.CONVERSATION_HIGH_LATENCY
        )

        with self.assertLogs("wallet.services.wallet", level="WARNING"):
            response = self.post_call(
                client=self.runner_for(broke.tenant), agent=agent,
                provider_call_id="call_cr04",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(self.log("call_cr04").credits_charged)

    def test_a_free_call_records_zero_not_null(self):
        """The other half of the rule: this call *was* rated, at nothing.

        Priced at zero through the wallet's own rate card rather than through a
        zero-rate plan. The plan factories translate legacy shapes into minute bands,
        and whether a zero credits-per-minute basis is valid is a separate question
        from the one this test asks.
        """
        wallet = self.wallet
        snapshot = wallet.rate_snapshot
        snapshot["operations"][Operation.CONVERSATION_HIGH_LATENCY]["credits_per_unit"] = 0
        wallet.rate_snapshot = snapshot
        wallet.save(update_fields=["rate_snapshot"])

        response = self.post_call(provider_call_id="call_cr05")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.log("call_cr05").credits_charged, 0)
        self.assertEqual(self.deductions().count(), 0)

    def test_a_wallet_failure_stays_null(self):
        with patch.object(
            wallet_service, "consume_detailed", side_effect=RuntimeError("wallet down")
        ):
            with self.assertLogs("core.views.agent_call_log", level="ERROR"):
                response = self.post_call(provider_call_id="call_cr06")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(self.log("call_cr06").credits_charged)

    def test_failing_to_record_the_total_does_not_fail_the_request(self):
        """The charge landed. The log must say the *recording* failed, not the charge,
        or someone refunds a call that was billed correctly."""
        with patch.object(
            AgentCallLog, "record_credits_charged", side_effect=RuntimeError("db hiccup")
        ):
            with self.assertLogs("core.views.agent_call_log", level="ERROR") as logs:
                response = self.post_call(provider_call_id="call_cr07")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.deductions().count(), 1)
        self.assertTrue(any("could not be recorded" in line for line in logs.output))
        self.assertFalse(any("failed to charge" in line for line in logs.output))


class ConsumeDetailedTests(CreditsChargedTestCase):
    def test_the_result_carries_the_total_every_row_and_the_primary_entry(self):
        self.top_up()

        result = wallet_service.consume_detailed(
            self.tenant, operation=Operation.CONVERSATION_HIGH_LATENCY,
            quantity=SPLIT_CALL_SECONDS, reference_id="call_cr10",
        )

        self.assertEqual(result.credits, SPLIT_CALL_CREDITS)
        self.assertEqual(len(result.entries), 2)
        self.assertIs(result.entry, result.entries[0])
        self.assertEqual(sum(abs(e.amount) for e in result.entries), result.credits)

    def test_consume_still_returns_the_primary_entry(self):
        """Every existing caller reads a ledger row off consume(). That must not move."""
        self.top_up()

        entry = wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_HIGH_LATENCY,
            quantity=SPLIT_CALL_SECONDS, reference_id="call_cr11",
        )

        self.assertIsInstance(entry, CreditLedger)
        self.assertEqual(entry.amount, -1000)

    def test_a_replay_returns_none_from_both(self):
        charge = dict(
            operation=Operation.CONVERSATION_HIGH_LATENCY, quantity=60,
            reference_id="call_cr12",
        )
        wallet_service.consume(self.tenant, **charge)

        self.assertIsNone(wallet_service.consume_detailed(self.tenant, **charge))
        self.assertIsNone(wallet_service.consume(self.tenant, **charge))


class BackfillTests(CreditsChargedTestCase):
    def run_backfill(self, *args):
        out = StringIO()
        call_command("backfill_call_credits", *args, stdout=out)
        return out.getvalue()

    def forget(self, *provider_call_ids):
        """Make rows look as they did before the field existed."""
        AgentCallLog.objects.filter(provider_call_id__in=provider_call_ids).update(
            credits_charged=None
        )

    def test_fills_historical_rows_including_a_split_charge(self):
        self.top_up()
        self.post_call(provider_call_id="call_bf01", duration_seconds=90)
        self.post_call(
            provider_call_id="call_bf02", duration_seconds=SPLIT_CALL_SECONDS
        )
        self.forget("call_bf01", "call_bf02")

        output = self.run_backfill()

        self.assertEqual(self.log("call_bf01").credits_charged, 10)
        self.assertEqual(self.log("call_bf02").credits_charged, SPLIT_CALL_CREDITS)
        self.assertIn("1 of them from a charge split across both buckets", output)

    def test_a_call_with_no_charge_stays_null(self):
        with patch.object(
            wallet_service, "consume_detailed", side_effect=RuntimeError("wallet down")
        ):
            with self.assertLogs("core.views.agent_call_log", level="ERROR"):
                self.post_call(provider_call_id="call_bf03")

        self.run_backfill()

        self.assertIsNone(self.log("call_bf03").credits_charged)

    def test_dry_run_changes_nothing(self):
        self.post_call(provider_call_id="call_bf04", duration_seconds=90)
        self.forget("call_bf04")

        self.run_backfill("--dry-run")

        self.assertIsNone(self.log("call_bf04").credits_charged)
