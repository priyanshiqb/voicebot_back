"""Section 6 of the billing test plan — WA-01 to WA-30.

Four read-mostly endpoints. The recurring theme is isolation: every one of them is
tenant-scoped, and a leak here shows one customer another customer's spending.
"""

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from onboard.tests.billing_factories import (
    BILLING,
    add_rates,
    make_plan,
    make_tenant,
)
from wallet.models import CreditLedger, CreditRate, Wallet
from wallet.services import wallet as wallet_service

Bucket = CreditLedger.Bucket
EntryType = CreditLedger.EntryType
Operation = CreditRate.Operation

BALANCE_URL = f"{BILLING}/wallet/"
RATES_URL = f"{BILLING}/wallet/rates/"
QUOTE_URL = f"{BILLING}/wallet/quote/"
LEDGER_URL = f"{BILLING}/wallet/ledger/"

RATES = {
    Operation.STATIC_REEL: 4,
    Operation.CONVERSATION_HIGH_LATENCY: 5,
    Operation.CONVERSATION_LOW_LATENCY: 10,
}


class WalletAPITestCase(TestCase):
    """One funded, stamped tenant, and a second one to prove isolation against."""

    def setUp(self):
        self.ctx = make_tenant("wallet-api")
        self.tenant = self.ctx.tenant
        self.client = self.ctx.client
        self.plan = make_plan("wallet-api-plan", rates=RATES, low_balance_threshold=100)
        wallet_service.set_plan_context(self.tenant, self.plan)
        wallet_service.grant(
            self.tenant, entry_type=EntryType.CYCLE_ALLOCATION,
            amount=1000, reference_id="wa-seed-allocated",
        )
        wallet_service.grant(
            self.tenant, entry_type=EntryType.MANUAL_TOP_UP,
            amount=500, bucket=Bucket.PURCHASED, reference_id="wa-seed-purchased",
        )

    def other_tenant(self):
        other = make_tenant("wallet-api-other")
        wallet_service.set_plan_context(other.tenant, self.plan)
        wallet_service.grant(
            other.tenant, entry_type=EntryType.CYCLE_ALLOCATION,
            amount=7777, reference_id="wa-other-seed",
        )
        return other


class WalletBalanceEndpointTests(WalletAPITestCase):
    """WA-01 to WA-06."""

    def test_wa01_returns_the_balance_and_both_buckets(self):
        """WA-01: the split matters — plan credits can expire at a cycle boundary
        and purchased ones never do, so a single total makes 'why did my balance
        drop at renewal?' unanswerable."""
        response = self.client.get(BALANCE_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()["data"]
        self.assertEqual(data["balance"], 1500)
        self.assertEqual(data["allocated_balance"], 1000)
        self.assertEqual(data["purchased_balance"], 500)
        self.assertTrue(data["is_spendable"])

    def test_wa02_anonymous_callers_are_refused(self):
        """WA-02."""
        response = APIClient().get(BALANCE_URL)

        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_wa03_each_tenant_sees_only_its_own_balance(self):
        """WA-03: **the leak that would matter most.**"""
        other = self.other_tenant()

        mine = self.client.get(BALANCE_URL).json()["data"]
        theirs = other.client.get(BALANCE_URL).json()["data"]

        self.assertEqual(mine["balance"], 1500)
        self.assertEqual(theirs["balance"], 7777)

    def test_wa04_a_user_with_no_tenant_is_refused(self):
        """WA-04: an unmapped user has no tenant context, and guessing one would be
        the worst possible answer."""
        from django.contrib.auth import get_user_model

        orphan = get_user_model().objects.create(
            email="orphan@example.com", username="orphan", is_active=True
        )
        client = APIClient()
        client.force_authenticate(user=orphan)

        response = client.get(BALANCE_URL)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_wa05_a_frozen_wallet_reports_why(self):
        """WA-05: 'you cannot spend' without a reason is a support ticket."""
        wallet_service.freeze(self.tenant, reason="subscription_expired")

        data = self.client.get(BALANCE_URL).json()["data"]

        self.assertTrue(data["is_frozen"])
        self.assertEqual(data["frozen_reason"], "subscription_expired")
        self.assertFalse(data["is_spendable"])

    def test_wa06_the_plan_code_comes_from_the_stamp(self):
        """WA-06."""
        data = self.client.get(BALANCE_URL).json()["data"]

        self.assertEqual(data["plan_code"], self.plan.code)
        self.assertEqual(data["plan_name"], self.plan.name)


class WalletRateCardEndpointTests(WalletAPITestCase):
    """WA-07 to WA-10."""

    def test_wa07_lists_every_stamped_tier_with_a_label(self):
        """WA-07: labels because a tenant reading 'conversation_low_latency' on a
        pricing screen is reading an internal identifier."""
        response = self.client.get(RATES_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()["data"]
        self.assertEqual(data["plan_code"], self.plan.code)
        self.assertEqual(len(data["operations"]), 3)

        by_operation = {row["operation"]: row for row in data["operations"]}
        self.assertEqual(
            by_operation[Operation.CONVERSATION_LOW_LATENCY]["credits_per_unit"], 10
        )
        self.assertTrue(
            by_operation[Operation.CONVERSATION_LOW_LATENCY]["label"].strip()
        )

    def test_wa08_the_response_reflects_the_stamp_not_the_catalogue(self):
        """WA-08: **the tenant sees the terms they bought.**

        Re-pricing the plan changes what future cycles cost; it must not silently
        rewrite what this tenant is being charged today.
        """
        rate = CreditRate.objects.get(
            plan=self.plan, operation=Operation.CONVERSATION_LOW_LATENCY
        )
        rate.credits_per_unit = 99
        rate.save(update_fields=["credits_per_unit"])

        data = self.client.get(RATES_URL).json()["data"]

        by_operation = {row["operation"]: row for row in data["operations"]}
        self.assertEqual(
            by_operation[Operation.CONVERSATION_LOW_LATENCY]["credits_per_unit"], 10
        )

    def test_wa09_an_unstamped_wallet_says_so_rather_than_erroring(self):
        """WA-09: a tenant mid-signup has no stamp yet, and a 500 on a dashboard
        widget is a worse answer than an empty one."""
        fresh = make_tenant("wallet-api-unstamped")
        wallet_service.for_tenant(fresh.tenant)

        response = fresh.client.get(RATES_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body["data"]["operations"], [])
        self.assertIsNone(body["data"]["plan_code"])
        self.assertIn("no rate card", body["message"].lower())

    def test_wa10_anonymous_callers_are_refused(self):
        """WA-10: prices are per-plan, so this is tenant data."""
        response = APIClient().get(RATES_URL)

        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )


class CreditQuoteEndpointTests(WalletAPITestCase):
    """WA-11 to WA-20."""

    def test_wa11_returns_the_price_and_the_arithmetic(self):
        """WA-11."""
        response = self.client.post(
            QUOTE_URL,
            {"operation": Operation.CONVERSATION_LOW_LATENCY, "quantity": 90},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()["data"]
        self.assertEqual(data["credits"], 20)
        self.assertEqual(data["units"], 2)
        self.assertEqual(data["raw_quantity"], 90)
        self.assertEqual(data["rate_applied"], 10)
        self.assertTrue(data["affordable"])

    def test_wa12_the_quote_is_the_price_actually_charged(self):
        """WA-12: **the whole reason the quote and the charge share one computation.**

        A quote a tenant is shown and then billed differently for is worse than no
        quote at all.
        """
        quoted = self.client.post(
            QUOTE_URL,
            {"operation": Operation.CONVERSATION_LOW_LATENCY, "quantity": 137},
            format="json",
        ).json()["data"]["credits"]

        entry = wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_LOW_LATENCY,
            quantity=137, reference_id="call-wa12",
        )

        self.assertEqual(abs(entry.amount), quoted)

    def test_wa13_a_quantity_of_zero_is_rejected(self):
        """WA-13."""
        response = self.client.post(
            QUOTE_URL,
            {"operation": Operation.CONVERSATION_LOW_LATENCY, "quantity": 0},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_wa14_a_negative_quantity_is_rejected(self):
        """WA-14."""
        response = self.client.post(
            QUOTE_URL,
            {"operation": Operation.CONVERSATION_LOW_LATENCY, "quantity": -60},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_wa15_a_missing_operation_is_rejected(self):
        """WA-15."""
        response = self.client.post(QUOTE_URL, {"quantity": 90}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("operation", response.json()["errors"])

    def test_wa16_an_unknown_operation_is_a_bad_request_not_a_conflict(self):
        """WA-16: a typo is the caller's mistake. Letting it fall through to the
        rating service would report it as 'this plan does not price that', which
        sends whoever is debugging to the plan configuration for nothing."""
        response = self.client.post(
            QUOTE_URL, {"operation": "teleportation", "quantity": 90}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("operation", response.json()["errors"])

    def test_wa17_a_valid_operation_with_no_rate_is_a_conflict_not_a_500(self):
        """WA-17: the request is well-formed; the server's state cannot answer it.
        409 is actionable by whoever configures plans, a 500 is actionable by nobody."""
        bare = make_tenant("wallet-api-bare")
        bare_plan = make_plan("wallet-api-bare-plan", with_rates=False)
        add_rates(bare_plan, {Operation.CONVERSATION_HIGH_LATENCY: 5})
        wallet_service.set_plan_context(bare.tenant, bare_plan)

        response = bare.client.post(
            QUOTE_URL,
            {"operation": Operation.CONVERSATION_LOW_LATENCY, "quantity": 90},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("RATE_NOT_CONFIGURED", response.json()["errors"])

    def test_wa18_an_unaffordable_quote_is_still_a_successful_quote(self):
        """WA-18: 'you cannot afford this' is the answer to the question, not a
        failure to answer it."""
        response = self.client.post(
            QUOTE_URL,
            {"operation": Operation.CONVERSATION_LOW_LATENCY, "quantity": 100000},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()["data"]
        self.assertFalse(data["affordable"])
        self.assertEqual(data["balance"], 1500)

    def test_wa19_a_frozen_wallet_can_afford_nothing(self):
        """WA-19: a funded but frozen wallet still cannot spend, and a quote that
        said otherwise would send the tenant into a refusal."""
        wallet_service.freeze(self.tenant, reason="subscription_expired")

        data = self.client.post(
            QUOTE_URL,
            {"operation": Operation.CONVERSATION_LOW_LATENCY, "quantity": 60},
            format="json",
        ).json()["data"]

        self.assertFalse(data["affordable"])
        self.assertEqual(data["balance"], 1500)

    def test_wa20_quoting_writes_no_ledger_row(self):
        """WA-20: a price check is not a purchase."""
        before = CreditLedger.all_objects.filter(tenant=self.tenant).count()

        self.client.post(
            QUOTE_URL,
            {"operation": Operation.CONVERSATION_LOW_LATENCY, "quantity": 90},
            format="json",
        )

        self.assertEqual(
            CreditLedger.all_objects.filter(tenant=self.tenant).count(), before
        )


class CreditLedgerEndpointTests(WalletAPITestCase):
    """WA-21 to WA-30."""

    def setUp(self):
        super().setUp()
        wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_LOW_LATENCY,
            quantity=90, reference_id="call-wa-a",
        )
        wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_HIGH_LATENCY,
            quantity=60, reference_id="call-wa-b",
        )

    def test_wa21_lists_this_tenants_rows_newest_first(self):
        """WA-21: a ledger read in creation order is a ledger nobody scrolls."""
        response = self.client.get(LEDGER_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = response.json()["data"]
        self.assertEqual(len(rows), 4)

        timestamps = [row["created_at"] for row in rows]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))

    def test_wa22_another_tenants_rows_never_appear(self):
        """WA-22: **strict isolation.** This endpoint is a spending history."""
        other = self.other_tenant()

        mine = self.client.get(LEDGER_URL).json()["data"]
        theirs = other.client.get(LEDGER_URL).json()["data"]

        self.assertEqual(len(theirs), 1)
        self.assertNotIn(
            "wa-other-seed", {row["reference_id"] for row in mine}
        )
        self.assertEqual(theirs[0]["reference_id"], "wa-other-seed")

    def test_wa23_a_usage_row_explains_its_own_charge(self):
        """WA-23: four numbers that only mean anything together — 90 seconds, per
        minute, 2 units, 10 credits each."""
        rows = self.client.get(
            LEDGER_URL, {"entry_type": EntryType.USAGE_DEDUCTION}
        ).json()["data"]
        row = next(r for r in rows if r["reference_id"] == "call-wa-a")

        self.assertEqual(
            set(row["rating"]),
            {"raw_quantity", "units", "unit_type", "rate_applied"},
        )
        self.assertEqual(row["rating"]["raw_quantity"], 90)
        self.assertEqual(row["rating"]["units"], 2)
        self.assertEqual(row["rating"]["rate_applied"], 10)

    def test_wa24_a_non_usage_row_carries_no_rating(self):
        """WA-24: five sibling nulls on a grant row would be noise, not data."""
        rows = self.client.get(
            LEDGER_URL, {"entry_type": EntryType.CYCLE_ALLOCATION}
        ).json()["data"]

        self.assertTrue(rows)
        for row in rows:
            self.assertIsNone(row["rating"])

    def test_wa25_filtering_by_entry_type(self):
        """WA-25."""
        rows = self.client.get(
            LEDGER_URL, {"entry_type": EntryType.MANUAL_TOP_UP}
        ).json()["data"]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["entry_type"], EntryType.MANUAL_TOP_UP)

    def test_wa26_filtering_by_bucket(self):
        """WA-26: 'what happened to my purchased credits' is a real question."""
        rows = self.client.get(LEDGER_URL, {"bucket": Bucket.PURCHASED}).json()["data"]

        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["bucket"], Bucket.PURCHASED)

    def test_wa27_filtering_by_direction(self):
        """WA-27."""
        credits = self.client.get(LEDGER_URL, {"is_credit": "true"}).json()["data"]
        debits = self.client.get(LEDGER_URL, {"is_credit": "false"}).json()["data"]

        self.assertTrue(credits and debits)
        for row in credits:
            self.assertGreater(row["amount"], 0)
        for row in debits:
            self.assertLess(row["amount"], 0)

    def test_wa28_filtering_by_a_date_window(self):
        """WA-28: the question this endpoint exists to answer is 'what did I spend
        last month'."""
        from datetime import timedelta

        from django.utils import timezone

        now = timezone.now()

        inside = self.client.get(
            LEDGER_URL,
            {"created_after": (now - timedelta(hours=1)).isoformat()},
        ).json()["data"]
        outside = self.client.get(
            LEDGER_URL,
            {"created_before": (now - timedelta(days=1)).isoformat()},
        ).json()["data"]

        self.assertEqual(len(inside), 4)
        self.assertEqual(outside, [])

    def test_wa29_pagination_metadata_sits_beside_the_rows(self):
        """WA-29: rows in ``data``, counts in ``meta`` — **not** ``data.results``.
        A client written against the wrong shape renders an empty table."""
        body = self.client.get(LEDGER_URL, {"page_size": 2}).json()

        self.assertIsInstance(body["data"], list)
        self.assertEqual(len(body["data"]), 2)
        self.assertEqual(body["meta"]["count"], 4)
        self.assertEqual(body["meta"]["page_size"], 2)
        self.assertIsNotNone(body["meta"]["next"])

    def test_wa30_every_write_verb_is_refused(self):
        """WA-30: the ledger is append-only, so there is nothing to expose beyond
        reading. Removing the verbs at the router beats overriding each handler."""
        row = CreditLedger.objects.filter(tenant=self.tenant).first()
        detail_url = f"{LEDGER_URL}{row.id}/"

        self.assertEqual(
            self.client.post(LEDGER_URL, {}, format="json").status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )
        self.assertEqual(
            self.client.put(detail_url, {}, format="json").status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )
        self.assertEqual(
            self.client.patch(detail_url, {}, format="json").status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )
        self.assertEqual(
            self.client.delete(detail_url).status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )

        # And nothing was written by any of them.
        self.assertEqual(
            CreditLedger.all_objects.filter(tenant=self.tenant).count(), 4
        )
