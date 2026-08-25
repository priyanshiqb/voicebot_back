"""Section 4 of the billing test plan — WP-01 to WP-09.

``policy`` is the only module that knows a call is billed per second of duration on
its service tier. Everything here is a pure function over a call log, so the tests use
unsaved model instances: real fields and real defaults, no fixture to maintain.
"""

from django.test import TestCase

from core.constants.service_tier import ServiceTier
from core.models.agent import Agent
from core.models.agent_call_log import AgentCallLog
from wallet.models import CreditRate
from wallet.services import policy


class TierResolutionTests(TestCase):
    """WP-01 to WP-03, WP-07 — which tier a call is billed at."""

    def test_wp01_the_call_log_beats_the_agent(self):
        """WP-01: **billed at what actually ran.**

        A bot configured for realtime may have been served on the standard model —
        a capacity fallback, a degraded provider. Charging the configured tier would
        bill the tenant for something they did not get.
        """
        agent = Agent(service_tier=ServiceTier.CONVERSATION_LOW_LATENCY)
        call_log = AgentCallLog(
            agent=agent, service_tier=ServiceTier.CONVERSATION_HIGH_LATENCY
        )

        self.assertEqual(
            policy.tier_for_call(call_log), ServiceTier.CONVERSATION_HIGH_LATENCY
        )

    def test_wp02_the_agent_is_the_fallback(self):
        """WP-02: runners that do not report a tier yet still bill correctly."""
        agent = Agent(service_tier=ServiceTier.CONVERSATION_LOW_LATENCY)
        call_log = AgentCallLog(agent=agent, service_tier=None)

        self.assertEqual(
            policy.tier_for_call(call_log), ServiceTier.CONVERSATION_LOW_LATENCY
        )

    def test_wp03_with_nothing_to_go_on_the_cheaper_tier_is_assumed(self):
        """WP-03: standard, not realtime. Guessing the most expensive tier would
        overcharge for a misconfiguration that is not the tenant's fault."""
        agent = Agent(service_tier=None)
        call_log = AgentCallLog(agent=agent, service_tier=None)

        self.assertEqual(policy.tier_for_call(call_log), policy.DEFAULT_TIER)
        self.assertEqual(policy.DEFAULT_TIER, ServiceTier.CONVERSATION_HIGH_LATENCY)
        self.assertNotEqual(
            policy.DEFAULT_TIER, ServiceTier.CONVERSATION_LOW_LATENCY
        )

    def test_wp07_a_call_with_no_agent_does_not_raise(self):
        """WP-07: an unset FK raises on attribute access in Django, so this is a
        real trap rather than a hypothetical one. Billing must not fail a request."""
        call_log = AgentCallLog(service_tier=None)

        self.assertEqual(policy.tier_for_call(call_log), policy.DEFAULT_TIER)


class OperationForCallTests(TestCase):
    """WP-04 to WP-06 — turning a call into ``(operation, quantity)``."""

    def test_wp04_a_zero_duration_call_is_not_billable(self):
        """WP-04: the runner posts a row before it knows how long the call ran.

        Rating zero would either charge a spurious minimum or write a zero-credit
        ledger row the non-zero constraint rejects. Neither is a charge anyone can
        defend, so it is left unrated for ``report_unrated_calls`` to surface.
        """
        call_log = AgentCallLog(
            service_tier=ServiceTier.CONVERSATION_HIGH_LATENCY, duration_seconds=0
        )

        self.assertIsNone(policy.operation_for_call(call_log))

    def test_wp05_a_null_duration_is_not_billable_either(self):
        """WP-05."""
        call_log = AgentCallLog(
            service_tier=ServiceTier.CONVERSATION_HIGH_LATENCY, duration_seconds=None
        )

        self.assertIsNone(policy.operation_for_call(call_log))

    def test_wp06_a_real_call_yields_its_tier_and_seconds(self):
        """WP-06: seconds, not minutes — the rate's unit decides how that becomes
        billable units, and this layer must not pre-empt it."""
        call_log = AgentCallLog(
            service_tier=ServiceTier.CONVERSATION_LOW_LATENCY, duration_seconds=90
        )

        self.assertEqual(
            policy.operation_for_call(call_log),
            (ServiceTier.CONVERSATION_LOW_LATENCY, 90),
        )


class TierDriftTests(TestCase):
    """WP-08 to WP-09 — the guard on two enums that must stay string-equal."""

    def test_wp08_every_service_tier_is_a_priceable_operation(self):
        """WP-08: **the drift guard.**

        ``ServiceTier`` and ``CreditRate.Operation`` are kept as separate enums so
        neither app imports the other, which means nothing but this test stops
        someone adding a tier that no plan can price. A call on such a tier would
        raise ``RateNotConfigured`` for every tenant on the platform.
        """
        tiers = {choice.value for choice in ServiceTier}
        operations = {choice.value for choice in CreditRate.Operation}

        self.assertEqual(
            tiers,
            operations,
            "ServiceTier and CreditRate.Operation have drifted apart; a tier with no "
            "matching operation cannot be priced by any plan.",
        )

    def test_wp09_agents_default_to_the_standard_tier(self):
        """WP-09: an agent created without a tier is the common case, and it must
        land on the same fallback the policy uses."""
        self.assertEqual(
            Agent().service_tier, ServiceTier.CONVERSATION_HIGH_LATENCY
        )
        self.assertEqual(Agent().service_tier, policy.DEFAULT_TIER)
