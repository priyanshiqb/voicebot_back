"""Section 19 of the billing test plan — CC-01 to CC-22.

Where credits actually get spent: a call log arriving, and a campaign starting.

The rule that governs the whole file: **billing must never fail the request.** The
call already happened and its log is already committed, so raising here would make the
runner retry a POST whose row exists — and every retry is a fresh attempt to charge
for the same call. A billing problem is ours to reconcile, not the runner's to retry
into. CC-07, CC-08 and CC-19 are that guarantee from three directions.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from core.constants.service_tier import ServiceTier
from core.models.agent_call_log import AgentCallLog
from core.models.campaign import Campaign
from core.services import campaigns as campaign_service
from onboard.services import subscriptions as subscription_service
from onboard.tests.billing_factories import (
    add_rates,
    assert_invariant,
    make_agent,
    make_plan,
    make_tenant,
)
from tenant.models import TenantSettings
from wallet.models import CreditLedger, CreditRate, Wallet
from wallet.services import wallet as wallet_service

User = get_user_model()

Bucket = CreditLedger.Bucket
EntryType = CreditLedger.EntryType
Operation = CreditRate.Operation

CALL_LOG_URL = "/api/v1/core/agent-call-logs/"
RUNNER_CONTEXT_URL = "/api/v1/core/campaign-runner/{}/context/"

RATES = {
    Operation.STATIC_REEL: 2,
    Operation.CONVERSATION_HIGH_LATENCY: 5,
    Operation.CONVERSATION_LOW_LATENCY: 10,
}


class ConsumptionTestCase(TestCase):
    """A funded tenant with a bot, ready to post call logs."""

    def setUp(self):
        self.ctx = make_tenant("consumption")
        self.tenant = self.ctx.tenant
        self.client = self.ctx.client
        self.plan = make_plan(
            "consumption-plan", rates=RATES, allocated_credits=1000
        )
        subscription_service.activate(self.tenant, self.plan)
        self.agent = make_agent(
            self.tenant, service_tier=ServiceTier.CONVERSATION_HIGH_LATENCY
        )
        # Only a VB_AGENT may create a call log: this endpoint exists for the runner,
        # and a tenant user posting their own usage would be self-reported billing.
        self.runner = self.runner_for(self.tenant)

    # --- helpers ------------------------------------------------------------

    def runner_for(self, tenant, suffix=""):
        """A machine client scoped to one tenant by the mandatory header."""
        machine = User.objects.create(
            email=f"runner{suffix}-{tenant.subdomain}@example.com",
            username=f"runner{suffix}-{tenant.subdomain}",
            user_type=User.UserType.VB_AGENT,
            is_active=True,
        )
        client = APIClient()
        client.force_authenticate(user=machine)
        client.credentials(HTTP_X_TENANT_ID=str(tenant.id))
        return client

    def post_call(self, client=None, **overrides):
        payload = {
            "provider_call_id": overrides.pop(
                "provider_call_id", f"call_{timezone.now().timestamp()}"
            ),
            "agent": str(overrides.pop("agent", self.agent).id),
            "caller_number": "+919900000001",
            "direction": AgentCallLog.CallDirection.OUTBOUND,
            "duration_seconds": 90,
        }
        payload.update(overrides)
        return (client or self.runner).post(CALL_LOG_URL, payload, format="json")

    def enforce(self, tenant=None, credits_enforced=True):
        settings_row = TenantSettings.for_tenant((tenant or self.tenant).id)
        settings_row.credits_enforced = credits_enforced
        settings_row.save(update_fields=["credits_enforced"])
        return settings_row

    @property
    def wallet(self):
        return Wallet.objects.get(tenant=self.tenant)

    def deductions(self, tenant=None):
        return CreditLedger.all_objects.filter(
            tenant=tenant or self.tenant, entry_type=EntryType.USAGE_DEDUCTION
        )


class CallChargeTests(ConsumptionTestCase):
    """CC-01 to CC-13."""

    def test_cc01_a_completed_call_is_charged_once(self):
        """CC-01."""
        response = self.post_call(provider_call_id="call_cc01")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.deductions().count(), 1)
        assert_invariant(self, self.tenant)

    def test_cc02_the_charge_matches_the_tier_and_the_duration(self):
        """CC-02: 90 seconds on a 5-credit tier, ceil, is 10 credits."""
        self.post_call(provider_call_id="call_cc02", duration_seconds=90)

        entry = self.deductions().get()
        self.assertEqual(abs(entry.amount), 10)
        self.assertEqual(self.wallet.balance, 990)

    def test_cc03_the_call_logs_tier_beats_the_agents(self):
        """CC-03: **billed at what actually ran.**

        A bot configured for realtime may have been served on the standard model —
        a capacity fallback, a degraded provider — and charging the configured tier
        would bill the tenant for something they did not get.
        """
        realtime_agent = make_agent(
            self.tenant, service_tier=ServiceTier.CONVERSATION_LOW_LATENCY
        )

        self.post_call(
            provider_call_id="call_cc03",
            agent=realtime_agent,
            duration_seconds=60,
            service_tier=ServiceTier.CONVERSATION_HIGH_LATENCY,
        )

        entry = self.deductions().get()
        self.assertEqual(entry.operation, Operation.CONVERSATION_HIGH_LATENCY)
        self.assertEqual(abs(entry.amount), 5)

    def test_cc04_without_a_tier_on_the_log_the_agents_is_used(self):
        """CC-04: for runners that do not report one yet."""
        realtime_agent = make_agent(
            self.tenant, service_tier=ServiceTier.CONVERSATION_LOW_LATENCY
        )

        self.post_call(
            provider_call_id="call_cc04", agent=realtime_agent, duration_seconds=60
        )

        entry = self.deductions().get()
        self.assertEqual(entry.operation, Operation.CONVERSATION_LOW_LATENCY)
        self.assertEqual(abs(entry.amount), 10)

    def test_cc05_a_re_posted_call_is_charged_once(self):
        """CC-05: **the runner is an external service that retries.**

        ``provider_call_id`` is already unique on the model, so this defence is free.
        """
        self.post_call(provider_call_id="call_cc05")
        balance = self.wallet.balance

        self.post_call(provider_call_id="call_cc05")

        self.assertEqual(self.deductions().count(), 1)
        self.assertEqual(self.wallet.balance, balance)

    def test_cc06_a_zero_duration_call_is_not_charged(self):
        """CC-06: **the runner posts a row before it knows how long the call ran.**

        Rating zero would charge a spurious minimum, so it is left for
        ``report_unrated_calls`` to surface rather than guessed at.
        """
        response = self.post_call(
            provider_call_id="call_cc06", duration_seconds=0
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.deductions().count(), 0)
        self.assertEqual(self.wallet.balance, 1000)

    def test_cc07_an_unpriceable_tier_does_not_fail_the_request(self):
        """CC-07: the log is the record of a call that happened; refusing to store
        it because we cannot price it loses the call *and* the money."""
        bare = make_tenant("consumption-unpriced")
        bare_plan = make_plan("consumption-unpriced-plan", with_rates=False)
        add_rates(bare_plan, {Operation.STATIC_REEL: 2})
        subscription_service.activate(bare, bare_plan)
        bare_agent = make_agent(
            bare.tenant, service_tier=ServiceTier.CONVERSATION_LOW_LATENCY
        )

        with self.assertLogs("core.views.agent_call_log", level="ERROR"):
            response = self.post_call(
                client=self.runner_for(bare.tenant), agent=bare_agent,
                provider_call_id="call_cc07",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            AgentCallLog.objects.filter(provider_call_id="call_cc07").exists()
        )

    def test_cc08_a_wallet_failure_does_not_fail_the_request(self):
        """CC-08: **anything unexpected is logged and swallowed.**"""
        with patch.object(
            wallet_service, "consume", side_effect=RuntimeError("wallet down")
        ):
            with self.assertLogs("core.views.agent_call_log", level="ERROR"):
                response = self.post_call(provider_call_id="call_cc08")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.deductions().count(), 0)

    def test_cc09_a_short_balance_records_the_call_and_charges_nothing(self):
        """CC-09: metering before blocking. The refusal is a log line, and the
        unrated-call sweep is what makes it visible."""
        # A plan that grants two credits: genuinely short, with a ledger that still
        # agrees with the balance. Breaking the wallet by hand would be testing the
        # bucket constraint rather than the shortfall.
        broke = make_tenant("consumption-broke")
        broke_plan = make_plan("consumption-broke-plan", rates=RATES)
        # A grant smaller than one minute of the plan's own rate is not something a
        # plan can describe -- the grant is minutes x credits-per-minute. Two credits
        # is an activation-time decision, the same override a negotiated deal uses.
        subscription_service.activate(broke, broke_plan, credit_grant=2)
        broke_agent = make_agent(
            broke.tenant, service_tier=ServiceTier.CONVERSATION_HIGH_LATENCY
        )

        with self.assertLogs("wallet.services.wallet", level="WARNING"):
            response = self.post_call(
                client=self.runner_for(broke.tenant), agent=broke_agent,
                provider_call_id="call_cc09",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.deductions(broke.tenant).count(), 0)
        self.assertEqual(Wallet.objects.get(tenant=broke.tenant).balance, 2)

    def test_cc10_the_ledger_row_explains_the_charge(self):
        """CC-10."""
        self.post_call(provider_call_id="call_cc10", duration_seconds=150)

        entry = self.deductions().get()
        self.assertEqual(entry.raw_quantity, 150)
        self.assertEqual(entry.units, 3)
        self.assertEqual(entry.unit_type, CreditRate.Unit.PER_MINUTE)
        self.assertEqual(entry.rate_applied, 5)
        self.assertEqual(abs(entry.amount), 15)

    def test_cc11_the_ledger_row_carries_the_call_context(self):
        """CC-11: enough to answer "which bot ran up this bill?" without joining
        back to a call log that may since have been purged."""
        self.post_call(provider_call_id="call_cc11")

        metadata = self.deductions().get().metadata
        self.assertEqual(metadata["agent_id"], str(self.agent.id))
        self.assertEqual(metadata["direction"], AgentCallLog.CallDirection.OUTBOUND)

    def test_cc12_the_charge_lands_on_the_call_logs_own_tenant(self):
        """CC-12: **the leak that would bill one customer for another's calls.**"""
        other = make_tenant("consumption-other")
        subscription_service.activate(other, self.plan)
        other_agent = make_agent(other.tenant)

        response = self.post_call(
            client=self.runner_for(other.tenant, suffix="-b"),
            agent=other_agent, provider_call_id="call_cc12",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.deductions().count(), 0)
        self.assertEqual(self.deductions(other.tenant).count(), 1)

    def test_cc13_a_machine_posted_call_is_charged_to_the_header_tenant(self):
        """CC-13: **the header is the authority on where this row lands**, and the
        charge has to follow the row."""
        client = self.runner_for(self.tenant, suffix="-c")

        response = self.post_call(client=client, provider_call_id="call_cc13")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.deductions().count(), 1)
        assert_invariant(self, self.tenant)


class CampaignPreflightTests(ConsumptionTestCase):
    """CC-14 to CC-19 — the check before a campaign dials anybody."""

    def scheduled_campaign(self, tenant=None, agent=None):
        return Campaign.objects.create(
            tenant=tenant or self.tenant,
            agent=agent or self.agent,
            name=f"campaign {timezone.now().timestamp()}".replace(".", ""),
            status=Campaign.Status.SCHEDULED,
            scheduled_start_at=timezone.now() - timedelta(minutes=5),
        )

    def test_cc14_a_funded_tenant_starts_their_campaign(self):
        """CC-14."""
        campaign = self.scheduled_campaign()
        self.enforce()

        campaign_service.start_due_campaigns()

        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.MATERIALIZING)

    def test_cc15_an_empty_wallet_does_not_stop_a_campaign_before_enforcement(self):
        """CC-15: **metering before blocking**, at the point it matters most — a
        wrong rate must not be able to stop a paying customer's campaign until the
        rates have been proven against real traffic."""
        Wallet.objects.filter(tenant=self.tenant).update(
            allocated_balance=0, balance=0
        )
        campaign = self.scheduled_campaign()

        with self.assertLogs("wallet.services.wallet", level="WARNING"):
            campaign_service.start_due_campaigns()

        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.MATERIALIZING)

    def test_cc16_an_empty_wallet_stops_a_campaign_once_enforcement_is_on(self):
        """CC-16: **a campaign that dials half its audience and stops cannot
        un-call the contacts it already reached.** It stays SCHEDULED so the next
        sweep picks it up once the tenant tops up."""
        Wallet.objects.filter(tenant=self.tenant).update(
            allocated_balance=0, balance=0
        )
        self.enforce()
        campaign = self.scheduled_campaign()

        with self.assertLogs("core.services.campaigns", level="WARNING"):
            campaign_service.start_due_campaigns()

        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.SCHEDULED)

    def test_cc17_a_frozen_wallet_stops_a_campaign_once_enforcement_is_on(self):
        """CC-17: a lapsed tenant with credits still in the wallet must not be able
        to spend them by starting a campaign."""
        wallet_service.freeze(self.tenant, reason="subscription_expired")
        self.enforce()
        campaign = self.scheduled_campaign()

        with self.assertLogs("core.services.campaigns", level="WARNING"):
            campaign_service.start_due_campaigns()

        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.SCHEDULED)

    def test_cc18_an_unpriced_tier_does_not_silently_skip_the_campaign(self):
        """CC-18: **a misconfiguration on our side must not look like a refusal.**

        Skipping here would leave the tenant with a campaign that never starts and
        no explanation anywhere they can see.
        """
        bare = make_tenant("consumption-campaign-unpriced")
        bare_plan = make_plan(
            "consumption-campaign-unpriced-plan", with_rates=False
        )
        add_rates(bare_plan, {Operation.STATIC_REEL: 2})
        subscription_service.activate(bare, bare_plan)
        self.enforce(bare.tenant)
        bare_agent = make_agent(
            bare.tenant, service_tier=ServiceTier.CONVERSATION_LOW_LATENCY
        )
        campaign = self.scheduled_campaign(bare.tenant, bare_agent)

        campaign_service.start_due_campaigns()

        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.MATERIALIZING)

    def test_cc19_a_broken_credit_check_does_not_stop_the_campaign(self):
        """CC-19: fail open. The charge still happens when each call log lands, so
        nothing is given away — only the pre-flight is skipped."""
        self.enforce()
        campaign = self.scheduled_campaign()

        with patch.object(
            wallet_service, "assert_can_consume",
            side_effect=RuntimeError("wallet unreachable"),
        ):
            with self.assertLogs("core.services.campaigns", level="ERROR"):
                campaign_service.start_due_campaigns()

        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.MATERIALIZING)


class RunnerContextTests(ConsumptionTestCase):
    """CC-20 to CC-21 — what the runner is told about credits."""

    def setUp(self):
        super().setUp()
        self.campaign = Campaign.objects.create(
            tenant=self.tenant,
            agent=self.agent,
            name="runner context campaign",
            status=Campaign.Status.RUNNING,
            scheduled_start_at=timezone.now(),
        )
    def context(self):
        return self.runner.get(RUNNER_CONTEXT_URL.format(self.campaign.pk))

    def test_cc20_the_runner_is_told_what_a_minute_costs_and_what_is_left(self):
        """CC-20: **advisory, not load-bearing.**

        A runner that ignores it is charged exactly the same; what this buys is a
        useful message instead of discovering an empty wallet one failed call at a
        time.
        """
        response = self.context()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        credits = response.json()["data"]["credits"]
        self.assertEqual(credits["balance"], 1000)
        self.assertFalse(credits["is_frozen"])
        self.assertEqual(credits["service_tier"], ServiceTier.CONVERSATION_HIGH_LATENCY)
        self.assertEqual(credits["credits_per_minute"], 5)

    def test_cc21_a_billing_lookup_failure_does_not_break_the_handoff(self):
        """CC-21: **the campaign still hands off.** ``credits: null`` is a missing
        convenience; a 500 here would stop the campaign entirely."""
        with patch.object(
            wallet_service, "for_tenant", side_effect=RuntimeError("wallet down")
        ):
            with self.assertLogs("core.views.campaign_runner", level="ERROR"):
                response = self.context()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()["data"]
        self.assertIsNone(body["credits"])
        self.assertIn("members", body)


class ConsumptionInvariantTests(ConsumptionTestCase):
    """CC-22."""

    def test_cc22_the_balance_still_matches_the_ledger_after_many_calls(self):
        """CC-22: **the assertion most likely to catch a regression nobody
        predicted** — run against the real API path rather than the service."""
        wallet_service.grant(
            self.tenant, entry_type=EntryType.MANUAL_TOP_UP, amount=500,
            bucket=Bucket.PURCHASED, reference_id="cc22-top-up",
        )

        for index in range(25):
            self.post_call(
                provider_call_id=f"call_cc22_{index}",
                duration_seconds=30 + index * 7,
            )
            assert_invariant(self, self.tenant, msg=f"after call {index}")

        wallet = self.wallet
        self.assertGreater(wallet.lifetime_consumed, 0)
        self.assertGreaterEqual(wallet.balance, 0)
