"""`GET /api/v1/ops/tenants/` — finding a tenant.

The list is the screen support reaches for first, so the filters are the contract.
Every assertion here is about which tenants come back and what the row says about
them; the cost of getting them back is asserted in ``test_tenant_query_budget``.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from onboard.tests.billing_factories import (
    make_call_log,
    make_plan,
    make_tenant,
    subscribe,
)
from ops.tests.tenant_console_factories import TENANTS, client_for, console, operator
from tenant.models import TenantSettings, TenantUserMapping
from user.models import CustomUser
from wallet.models import Wallet

Role = CustomUser.PlatformRole


def rows(response):
    return response.json()["data"]


def names(response):
    return {row["name"] for row in rows(response)}


class AccessTests(TestCase):
    """The namespace gate, on a read."""

    def setUp(self):
        self.plan = make_plan("access")

    def test_anonymous_is_refused(self):
        from rest_framework.test import APIClient

        self.assertIn(APIClient().get(TENANTS).status_code, (401, 403))

    def test_tenant_user_is_refused(self):
        """However privileged inside their own tenant, they are not platform staff."""
        ctx = make_tenant("customer")
        self.assertEqual(ctx.client.get(TENANTS).status_code, 403)

    def test_sub_manager_may_read(self):
        """A read names no permission, so any platform staff reaches it.

        Deliberate: the namespace gate has already run, and a console where support
        cannot look at a tenant is not a console.
        """
        client = console("sub@ops.com", role=Role.SUB_MANAGER)
        self.assertEqual(client.get(TENANTS).status_code, 200)

    def test_superuser_without_platform_role_is_refused(self):
        """`is_superuser` grants Django admin, which is a different thing."""
        user = CustomUser.objects.create(
            email="root@example.com", username="root", is_superuser=True, is_staff=True,
        )
        self.assertEqual(client_for(user).get(TENANTS).status_code, 403)


class ColumnTests(TestCase):
    def setUp(self):
        self.client_ops = console()
        self.plan = make_plan("columns", name="Growth")
        self.ctx = make_tenant("acme")
        self.subscription = subscribe(self.ctx, self.plan)

    def test_row_carries_every_list_column(self):
        row = rows(self.client_ops.get(TENANTS))[0]
        for field in (
            "id", "name", "subdomain", "created_at", "is_active", "plan", "plan_code",
            "subscription_status", "cycle_ends", "has_scheduled_change", "balance",
            "wallet_frozen", "credits_enforced", "entitlements_enforced",
            "last_call_at",
        ):
            self.assertIn(field, row, f"missing column {field}")

    def test_plan_comes_from_the_snapshot_not_the_live_plan(self):
        """A rename must not rewrite what a customer is shown they are paying for."""
        snapshot_name = self.subscription.plan_snapshot["name"]
        self.plan.name = "Renamed After Purchase"
        self.plan.save(update_fields=["name"])

        row = rows(self.client_ops.get(TENANTS))[0]
        self.assertEqual(row["plan"], snapshot_name)
        self.assertNotEqual(row["plan"], "Renamed After Purchase")

    def test_balance_reflects_the_wallet(self):
        wallet = Wallet.objects.get(tenant=self.ctx.tenant)
        self.assertEqual(rows(self.client_ops.get(TENANTS))[0]["balance"], wallet.balance)

    def test_last_call_is_the_most_recent(self):
        older = timezone.now() - timedelta(days=3)
        make_call_log(self.ctx, started_at=older)
        newest = timezone.now() - timedelta(hours=1)
        make_call_log(self.ctx, started_at=newest)

        row = rows(self.client_ops.get(TENANTS))[0]
        self.assertIsNotNone(row["last_call_at"])
        self.assertAlmostEqual(
            parse_datetime(row["last_call_at"]), newest, delta=timedelta(seconds=1)
        )

    def test_tenant_with_nothing_configured_still_renders(self):
        """No subscription, no wallet, no settings row. The interesting row."""
        make_tenant("bare")
        row = next(r for r in rows(self.client_ops.get(TENANTS)) if r["name"] == "bare")

        self.assertIsNone(row["plan"])
        self.assertIsNone(row["subscription_status"])
        self.assertEqual(row["balance"], 0)
        self.assertFalse(row["wallet_frozen"])
        self.assertFalse(row["credits_enforced"])
        self.assertFalse(row["entitlements_enforced"])


class FilterTests(TestCase):
    def setUp(self):
        self.client_ops = console()
        self.plan = make_plan("filters")
        self.other_plan = make_plan("filters-other")

        self.subscribed = make_tenant("subscribed")
        subscribe(self.subscribed, self.plan)

        self.bare = make_tenant("bare")

        self.frozen = make_tenant("frozen")
        subscribe(self.frozen, self.other_plan)
        Wallet.objects.filter(tenant=self.frozen.tenant).update(
            is_frozen=True, frozen_reason="fraud_hold"
        )

    def test_search_matches_name(self):
        self.assertEqual(names(self.client_ops.get(TENANTS, {"q": "froz"})), {"frozen"})

    def test_search_matches_subdomain(self):
        subdomain = self.bare.tenant.subdomain
        self.assertEqual(names(self.client_ops.get(TENANTS, {"q": subdomain})), {"bare"})

    def test_search_matches_owner_email(self):
        email = self.subscribed.user.email
        self.assertEqual(names(self.client_ops.get(TENANTS, {"q": email})), {"subscribed"})

    def test_search_does_not_duplicate_a_tenant_with_several_matching_users(self):
        """The one place a row can multiply: the join through `user_mappings`."""
        for i in range(3):
            user = CustomUser.objects.create(
                email=f"shared-term-{i}@example.com", username=f"shared-term-{i}",
            )
            TenantUserMapping.objects.create(user=user, tenant=self.bare.tenant)

        response = self.client_ops.get(TENANTS, {"q": "shared-term"})
        self.assertEqual(len(rows(response)), 1)
        self.assertEqual(response.json()["meta"]["count"], 1)

    def test_status_none_finds_tenants_with_no_subscription(self):
        """Mid-onboarding, or a payment that never completed."""
        self.assertEqual(names(self.client_ops.get(TENANTS, {"status": "none"})), {"bare"})

    def test_status_active_finds_subscribed_tenants(self):
        self.assertEqual(
            names(self.client_ops.get(TENANTS, {"status": "active"})),
            {"subscribed", "frozen"},
        )

    def test_status_expired_is_not_confused_with_never_subscribed(self):
        """A lapsed tenant is a different problem from one who never signed up."""
        from onboard.services import subscriptions as subscription_service
        from onboard.tests.billing_factories import expire_subscription

        expire_subscription(subscription_service.current_for(self.subscribed.tenant))

        self.assertEqual(
            names(self.client_ops.get(TENANTS, {"status": "expired"})), {"subscribed"}
        )
        self.assertEqual(names(self.client_ops.get(TENANTS, {"status": "none"})), {"bare"})

    def test_plan_filter_matches_the_snapshot(self):
        self.assertEqual(
            names(self.client_ops.get(TENANTS, {"plan": self.plan.code})), {"subscribed"}
        )

    def test_frozen_filter(self):
        self.assertEqual(names(self.client_ops.get(TENANTS, {"frozen": "true"})), {"frozen"})
        self.assertEqual(
            names(self.client_ops.get(TENANTS, {"frozen": "false"})), {"subscribed", "bare"}
        )

    def test_enforcement_filters(self):
        settings_row = TenantSettings.for_tenant(self.subscribed.tenant.id)
        settings_row.credits_enforced = True
        settings_row.save(update_fields=["credits_enforced"])

        self.assertEqual(
            names(self.client_ops.get(TENANTS, {"enforcement": "credits"})), {"subscribed"}
        )
        self.assertEqual(names(self.client_ops.get(TENANTS, {"enforcement": "both"})), set())
        # A tenant with no settings row has neither -- its subquery is NULL, not False.
        self.assertEqual(
            names(self.client_ops.get(TENANTS, {"enforcement": "neither"})),
            {"bare", "frozen"},
        )

    def test_is_active_filter(self):
        self.bare.tenant.deactivate()
        self.assertEqual(names(self.client_ops.get(TENANTS, {"is_active": "false"})), {"bare"})


class SoftDeleteTests(TestCase):
    def setUp(self):
        self.client_ops = console()
        self.kept = make_tenant("kept")
        self.gone = make_tenant("gone")
        self.gone.tenant.soft_delete()

    def test_deleted_tenants_are_excluded_by_default(self):
        self.assertEqual(names(self.client_ops.get(TENANTS)), {"kept"})

    def test_include_deleted_finds_them(self):
        """"This tenant says their account vanished" is why the console exists."""
        response = self.client_ops.get(TENANTS, {"include_deleted": "true"})
        self.assertEqual(names(response), {"kept", "gone"})
        row = next(r for r in rows(response) if r["name"] == "gone")
        self.assertTrue(row["is_deleted"])
        self.assertIsNotNone(row["deleted_at"])


class OrderingAndPaginationTests(TestCase):
    def setUp(self):
        self.client_ops = console()
        self.tenants = [make_tenant(f"t{i}") for i in range(3)]

    def test_default_ordering_is_newest_first(self):
        self.assertEqual(
            [row["name"] for row in rows(self.client_ops.get(TENANTS))],
            ["t2", "t1", "t0"],
        )

    def test_ordering_by_name(self):
        self.assertEqual(
            [row["name"] for row in rows(self.client_ops.get(TENANTS, {"ordering": "name"}))],
            ["t0", "t1", "t2"],
        )

    def test_unknown_ordering_falls_back_to_the_default(self):
        """A whitelist, so an unindexed column cannot be sorted on by asking."""
        self.assertEqual(
            [
                row["name"]
                for row in rows(self.client_ops.get(TENANTS, {"ordering": "; DROP"}))
            ],
            ["t2", "t1", "t0"],
        )

    def test_paginates_with_the_project_envelope(self):
        response = self.client_ops.get(TENANTS, {"page_size": 2})
        body = response.json()
        self.assertTrue(body["status"])
        self.assertEqual(len(body["data"]), 2)
        self.assertEqual(body["meta"]["count"], 3)
        self.assertIsNotNone(body["meta"]["next"])
