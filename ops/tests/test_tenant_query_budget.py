"""What the console costs, as exact numbers.

Following `wallet/tests/test_query_budget.py`: **every budget is an exact number, not
a ceiling.** A budget that only fails when it doubles is a budget nobody notices
breaking. When one of these fails, the fix is either to restore the query count or to
change the number here deliberately — never to loosen it to a range.

The list budget is the one that matters. A list that silently becomes five queries per
row still *works*; it just gets slower every month, which is the failure mode nobody
catches by looking at the screen.
"""

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from onboard.tests.billing_factories import (
    make_call_log,
    make_plan,
    make_tenant,
    make_transaction,
    subscribe,
)
from ops.tests.tenant_console_factories import TENANTS, console, detail_url
from tenant.models import TenantSettings

#: One statement for the page, one for the pagination count. Nothing per row.
LIST_QUERIES = 2

#: The 360, honestly. One screen, one tenant, opened by a human. The repeats are all
#: accounted for and none is per-row:
#:
#:   3  tenant_tenantusermapping  owner, user count, USER entitlement usage
#:   4  wallet_credit_ledger      ledger rows, credits burned, unrated set, reconcile
#:   2  onboard_subscription      current_or_expired, then the select_related read
#:   2  core_campaign             usage rollup, CAMPAIGN_MONTHLY entitlement usage
#:   2  onboard_transaction       payments block, failed-payments check
#:   2  core_agent_call_log       usage rollup, unrated-calls check
#:   1  each                      tenant, settings, wallet, entitlement extras,
#:                                and the five remaining live usage counters
DETAIL_QUERIES = 24


def warm(client):
    """Spend the one query that belongs to the middleware, not to the endpoint.

    `config.middleware` resolves `request.user.tenant_mapping` on **every**
    authenticated request, project-wide. A platform operator has no mapping, so it
    always misses and is swallowed — one query, on every endpoint in the project,
    including the plan console's.

    Django caches that failed reverse lookup on the user instance, and
    `force_authenticate` reuses one instance for the life of the client. So the first
    request through a client pays it and the rest do not, which would make these
    budgets depend on how many requests a test happened to make first. Spending it up
    front measures what the endpoint costs.
    """
    client.get(TENANTS)
    return client


def queries(fn):
    with CaptureQueriesContext(connection) as captured:
        fn()
    return captured


class ListBudgetTests(TestCase):
    """Two queries, at any page size and any number of tenants."""

    def setUp(self):
        self.client_ops = warm(console())
        self.plan = make_plan("budget")

    def populate(self, count):
        for i in range(count):
            ctx = make_tenant(f"b{i}")
            subscribe(ctx, self.plan)
            make_call_log(ctx)
            TenantSettings.for_tenant(ctx.tenant.id)

    def test_list_costs_two_queries(self):
        self.populate(5)
        captured = queries(lambda: self.client_ops.get(TENANTS, {"page_size": 5}))
        self.assertEqual(len(captured), LIST_QUERIES, [q["sql"] for q in captured])

    def test_the_cost_does_not_grow_with_the_number_of_rows(self):
        """The assertion a per-row leak fails even when somebody raised the first one."""
        self.populate(5)
        five = queries(lambda: self.client_ops.get(TENANTS, {"page_size": 25}))

        self.populate(15)
        twenty = queries(lambda: self.client_ops.get(TENANTS, {"page_size": 25}))

        self.assertEqual(len(five), LIST_QUERIES, [q["sql"] for q in five])
        self.assertEqual(len(twenty), LIST_QUERIES, [q["sql"] for q in twenty])

    def test_filtering_and_searching_do_not_add_queries(self):
        self.populate(5)
        captured = queries(
            lambda: self.client_ops.get(
                TENANTS, {"q": "b", "status": "active", "ordering": "-balance"}
            )
        )
        self.assertEqual(len(captured), LIST_QUERIES, [q["sql"] for q in captured])

    def test_no_entitlement_summary_runs_on_the_list_path(self):
        """`summary(with_usage=True)` is 8 queries per tenant. It must never be here.

        Asserted on the tables rather than the count, so this keeps holding if the
        budget above is ever legitimately changed.
        """
        self.populate(3)
        captured = queries(lambda: self.client_ops.get(TENANTS))
        sql = " ".join(q["sql"] for q in captured)
        for table in ("onboard_tenant_entitlement", "core_campaign", "wallet_credit_rate"):
            self.assertNotIn(table, sql, f"{table} reached the list path")


class DetailBudgetTests(TestCase):
    def setUp(self):
        self.client_ops = warm(console())
        self.plan = make_plan("budget-detail")
        self.ctx = make_tenant("detail")
        subscribe(self.ctx, self.plan)
        TenantSettings.for_tenant(self.ctx.tenant.id)
        make_call_log(self.ctx)
        make_transaction(self.ctx, self.plan)

    def test_the_360_costs_a_fixed_number_of_queries(self):
        captured = queries(lambda: self.client_ops.get(detail_url(self.ctx.tenant)))
        self.assertEqual(
            len(captured), DETAIL_QUERIES, "\n".join(q["sql"] for q in captured)
        )

    def test_the_cost_does_not_grow_with_history(self):
        """Ten more payments and ten more calls are still the same slice."""
        for _ in range(10):
            make_transaction(self.ctx, self.plan)
            make_call_log(self.ctx)

        captured = queries(lambda: self.client_ops.get(detail_url(self.ctx.tenant)))
        self.assertEqual(
            len(captured), DETAIL_QUERIES, "\n".join(q["sql"] for q in captured)
        )

    def test_the_middleware_adds_exactly_one_query_on_a_cold_user(self):
        """Documented, not hidden: `warm()` removes a cost that production still pays.

        It is one query per request, project-wide, and it is not this module's to
        fix — but a budget that quietly excludes it should say so out loud.
        """
        cold = console("cold@ops.com")
        captured = queries(lambda: cold.get(detail_url(self.ctx.tenant)))
        self.assertEqual(len(captured), DETAIL_QUERIES + 1)

    def test_a_tenant_with_nothing_configured_is_not_more_expensive(self):
        """The degraded path must not become a fallback that costs extra lookups."""
        bare = make_tenant("bare")
        captured = queries(lambda: self.client_ops.get(detail_url(bare.tenant)))
        self.assertLessEqual(
            len(captured), DETAIL_QUERIES, "\n".join(q["sql"] for q in captured)
        )
