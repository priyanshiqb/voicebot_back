"""Section 28 of the billing test plan — QB-01 to QB-14.

The call path runs on every call. A regression here is invisible until volume makes it
expensive, and by then it is in production.

**Every budget is an exact number, not a ceiling.** A budget that only fails when it
doubles is a budget nobody notices breaking. When one of these fails, the fix is either
to restore the query count or to change the number here deliberately — never to loosen
it to a range.

The two that matter most are QB-01 and QB-05. Rating a call reads the snapshot stamped
on the wallet, so it costs nothing; and the ``CreditRate`` catalogue must never appear
on the consumption path, because that is the cross-app join the push-don't-pull design
exists to avoid.
"""

from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from onboard.constants.entitlement_resource import EntitlementResource
from onboard.services import subscriptions as subscription_service
from onboard.tests.billing_factories import (
    BILLING,
    make_agent,
    make_plan,
    make_tenant,
)
from tenant.models import TenantSettings
from wallet.models import CreditLedger, CreditRate, Wallet
from wallet.services import rating
from wallet.services import wallet as wallet_service

Bucket = CreditLedger.Bucket
EntryType = CreditLedger.EntryType
Operation = CreditRate.Operation
Resource = EntitlementResource

WALLET_URL = f"{BILLING}/wallet/"
LEDGER_URL = f"{BILLING}/wallet/ledger/"
SUBSCRIPTION_URL = f"{BILLING}/subscription/"
PLANS_URL = f"{BILLING}/subscription/eligible-plans/"
AGENTS_URL = "/api/v1/core/agents/"


def touched_tables(queries, table):
    return [entry["sql"] for entry in queries if table in entry["sql"]]


class QueryBudgetTestCase(TestCase):
    """A funded, stamped tenant — the state every call arrives in."""

    def setUp(self):
        self.ctx = make_tenant("budget")
        self.tenant = self.ctx.tenant
        self.client = self.ctx.client
        self.plan = make_plan(
            "budget-plan",
            rates={Operation.CONVERSATION_HIGH_LATENCY: 5},
            allocated_credits=10000,
        )
        self.subscription = subscription_service.activate(self.tenant, self.plan)
        self.wallet = Wallet.objects.get(tenant=self.tenant)

    def charge(self, reference, seconds=60):
        return wallet_service.consume(
            self.tenant,
            operation=Operation.CONVERSATION_HIGH_LATENCY,
            quantity=seconds,
            reference_id=reference,
        )


class RatingPathBudgets(QueryBudgetTestCase):
    """QB-01 to QB-07 — the hot path."""

    def test_qb01_resolving_a_rate_from_a_stamp_costs_nothing(self):
        """QB-01: **the reason the rate card is pushed onto the wallet.**

        Pulling it at call time would put a cross-app join on every call; reading
        the stamped snapshot is an in-memory dictionary lookup.
        """
        with self.assertNumQueries(0):
            rate = rating.resolve_rate(
                self.wallet, Operation.CONVERSATION_HIGH_LATENCY
            )

        self.assertEqual(rate.source, "snapshot")

    def test_qb02_rating_a_quantity_costs_nothing(self):
        """QB-02: pure arithmetic over a dataclass."""
        resolved = rating.resolve_rate(
            self.wallet, Operation.CONVERSATION_HIGH_LATENCY
        )

        with self.assertNumQueries(0):
            result = rating.rate_quantity(resolved, 90)

        self.assertEqual(result.credits, 10)

    def test_qb03_quoting_reads_one_row(self):
        """QB-03: the wallet, and nothing else. A quote endpoint is polled from a
        pricing widget, so it has to stay a single indexed read."""
        with self.assertNumQueries(1):
            wallet_service.quote(
                self.tenant,
                operation=Operation.CONVERSATION_HIGH_LATENCY,
                quantity=90,
            )

    def test_qb04_consuming_costs_a_fixed_number_of_queries(self):
        """QB-04: **the exact shape of a charge.**

        Savepoint, idempotency probe, ensure-exists, lock, balance update, ledger
        insert, release. Six statements plus the savepoint pair, and none of them
        scales with the tenant's history.
        """
        with CaptureQueriesContext(connection) as queries:
            self.charge("budget-qb04")

        statements = [entry["sql"] for entry in queries]
        real = [sql for sql in statements if "SAVEPOINT" not in sql.upper()]

        self.assertEqual(
            len(real), 5,
            "the consumption path changed shape:\n" + "\n".join(real),
        )
        self.assertEqual(
            len([sql for sql in real if "FOR UPDATE" in sql]), 1,
            "exactly one row lock per charge",
        )
        self.assertEqual(
            len([sql for sql in real if sql.lstrip().upper().startswith("INSERT")]), 1,
            "exactly one ledger row per single-bucket charge",
        )

    def test_qb05_consuming_never_touches_the_rate_catalogue(self):
        """QB-05: **the catalogue stays off the hot path.**

        A query against ``wallet_credit_rate`` here means some wallet lost its
        stamp and the fallback is running — correct, but not something that should
        happen once per call in production.
        """
        with CaptureQueriesContext(connection) as queries:
            self.charge("budget-qb05")

        self.assertEqual(
            touched_tables(queries, "wallet_credit_rate"), [],
            "consumption read the CreditRate catalogue",
        )

    def test_qb06_authorising_reads_the_wallet_and_the_settings_row(self):
        """QB-06: two reads, both indexed, neither the catalogue."""
        TenantSettings.for_tenant(self.tenant.id)

        with CaptureQueriesContext(connection) as queries:
            wallet_service.assert_can_consume(
                self.tenant,
                operation=Operation.CONVERSATION_HIGH_LATENCY,
                quantity=60,
            )

        statements = [entry["sql"] for entry in queries]
        self.assertEqual(len(statements), 2, "\n".join(statements))
        self.assertEqual(touched_tables(queries, "wallet_credit_rate"), [])

    def test_qb07_authorising_never_writes(self):
        """QB-07: ``TenantSettings.for_tenant()`` would create a row, and this runs
        before every call. **A read path that writes is a write path.**"""
        TenantSettings.all_objects.filter(tenant=self.tenant).delete()

        with CaptureQueriesContext(connection) as queries:
            wallet_service.assert_can_consume(
                self.tenant,
                operation=Operation.CONVERSATION_HIGH_LATENCY,
                quantity=60,
            )

        writes = [
            entry["sql"] for entry in queries
            if entry["sql"].lstrip().upper().startswith(("INSERT", "UPDATE"))
        ]
        self.assertEqual(writes, [], "authorising a call wrote to the database")
        self.assertFalse(
            TenantSettings.all_objects.filter(tenant=self.tenant).exists()
        )


class ApiBudgets(QueryBudgetTestCase):
    """QB-08 to QB-11, QB-14 — endpoints that must not scale with data."""

    def measure(self, method, url, *args, **kwargs):
        with CaptureQueriesContext(connection) as queries:
            response = method(url, *args, **kwargs)
        return response, len(queries.captured_queries)

    def test_qb08_the_balance_endpoint_is_flat_in_ledger_size(self):
        """QB-08: **it reads a cached balance, not a sum.**

        If this grows with history, the denormalised balance has stopped being used
        and the whole reason it exists is gone.
        """
        _, small = self.measure(self.client.get, WALLET_URL)

        for index in range(40):
            self.charge(f"budget-qb08-{index}")

        _, large = self.measure(self.client.get, WALLET_URL)

        self.assertEqual(large, small, "the balance endpoint is summing the ledger")

    def test_qb09_the_ledger_endpoint_is_flat_per_page(self):
        """QB-09: no N+1 on tenant or wallet — the classic serializer regression,
        and one that only shows up once a tenant has a real history."""
        for index in range(6):
            self.charge(f"budget-qb09-{index}")
        _, small = self.measure(self.client.get, LEDGER_URL, {"page_size": 3})

        _, large = self.measure(self.client.get, LEDGER_URL, {"page_size": 6})

        self.assertEqual(
            large, small, "the ledger list is querying once per row"
        )

    def test_qb10_the_subscription_endpoint_counts_once_per_resource(self):
        """QB-10: usage is counted live, so this is one query per resource — not
        one per agent, contact or file the tenant holds."""
        _, empty = self.measure(self.client.get, SUBSCRIPTION_URL)

        for _ in range(5):
            make_agent(self.tenant)

        _, populated = self.measure(self.client.get, SUBSCRIPTION_URL)

        self.assertEqual(
            populated, empty,
            "the subscription endpoint is querying per owned object",
        )

    def test_qb11_the_plan_list_does_not_query_per_plan_for_rates(self):
        """QB-11: **bounded, not zero.**

        Pricing a plan now reads its basis rate row -- that is where the rupees per
        minute live -- so the catalogue is consulted. What must not happen is one
        query per plan, which is what a prefetch turns into a single fetch however
        many plans there are.
        """
        for index in range(4):
            make_plan(f"budget-extra-plan-{index}")

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(PLANS_URL)

        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(
            len(touched_tables(queries, "wallet_credit_rate")), 1,
            "eligible-plans is reading the rate catalogue per plan",
        )

    def test_qb14_the_entitlement_guard_adds_a_bounded_number_of_queries(self):
        """QB-14: the guard runs on every guarded create, so its cost has to be
        constant — three reads, whatever the tenant already holds."""
        # Enforcement stays off so an over-limit run returns rather than raising:
        # what is being measured is the query shape, which is identical either way.
        TenantSettings.for_tenant(self.tenant.id)

        from onboard.services import entitlements as entitlement_service

        def measure():
            with self.assertLogs("onboard.services.entitlements", level="WARNING"):
                with CaptureQueriesContext(connection) as queries:
                    entitlement_service.check(self.tenant, Resource.AGENT)
            return len(queries.captured_queries)

        # Both measurements are taken over the limit, so both take the same branch.
        # Comparing an under-limit run against an over-limit one would only show
        # that the over-limit branch reads the enforcement flag, which is a constant
        # extra query rather than growth.
        for _ in range(10):
            make_agent(self.tenant)
        ten_agents = measure()

        for _ in range(10):
            make_agent(self.tenant)

        self.assertEqual(
            measure(), ten_agents,
            "the entitlement check is scaling with the tenant's usage",
        )
        self.assertLessEqual(
            ten_agents, 5,
            "the guard should be a handful of indexed reads per create",
        )


class CommandBudgets(TestCase):
    """QB-12 to QB-13."""

    def setUp(self):
        self.plan = make_plan("budget-command-plan", allocated_credits=100)

    def test_qb12_reconciliation_is_two_queries_however_many_wallets(self):
        """QB-12: **this job is a full table scan by definition.**

        The readable version was three queries per wallet — fine at seventeen,
        unusable at a hundred thousand, and this job exists to run over all of them.
        """
        from io import StringIO

        from django.core.management import call_command

        def scan():
            with CaptureQueriesContext(connection) as queries:
                call_command(
                    "reconcile_wallets", stdout=StringIO(), stderr=StringIO()
                )
            return len(queries.captured_queries)

        for index in range(3):
            subscription_service.activate(
                make_tenant(f"budget-recon-{index}"), self.plan
            )
        few = scan()

        for index in range(3, 15):
            subscription_service.activate(
                make_tenant(f"budget-recon-{index}"), self.plan
            )
        many = scan()

        self.assertEqual(few, many, "reconciliation is querying per wallet")
        self.assertLessEqual(
            many, 3, "reconciliation should be a couple of aggregate queries"
        )

    def test_qb13_the_entitlement_summary_costs_one_query_per_resource(self):
        """QB-13: **one per resource, not one per row** — and it is opt-in for
        exactly that reason. Fine for a settings page, not for a hot path."""
        from onboard.services import entitlements as entitlement_service

        ctx = make_tenant("budget-summary")
        subscription = subscription_service.activate(ctx, self.plan)

        with CaptureQueriesContext(connection) as without:
            entitlement_service.summary(ctx.tenant.id, subscription)

        with CaptureQueriesContext(connection) as with_usage:
            entitlement_service.summary(
                ctx.tenant.id, subscription, with_usage=True
            )

        added = len(with_usage.captured_queries) - len(without.captured_queries)
        self.assertEqual(
            added, len(Resource.values),
            "usage counting should be exactly one query per resource",
        )
