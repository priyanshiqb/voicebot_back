"""Section 13 of the billing test plan — BS-01 to BS-52.

The endpoints a tenant manages their own plan through. §13.1 covers the lapsed case,
where the rule is **see everything, buy only a plan**: an expired tenant needs to
understand why they are blocked and what it costs to return, but renting capacity for
a cycle that is not running does not.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from onboard.constants.entitlement_resource import EntitlementResource
from onboard.models import Plan, Subscription, TenantEntitlement, Transaction
from onboard.services import pricing
from onboard.services import subscriptions as subscription_service
from onboard.tests.billing_factories import (
    BILLING,
    make_free_plan,
    make_plan,
    make_tenant,
    pay,
)
from onboard.views.billing.subscription import RENEWAL_WINDOW_DAYS
from wallet.models import CreditLedger, Wallet
from wallet.services import wallet as wallet_service

Bucket = CreditLedger.Bucket
EntryType = CreditLedger.EntryType
Resource = EntitlementResource
Status = Subscription.SubscriptionStatusChoices
TxnStatus = Transaction.TransactionStatusChoices
TxnType = Transaction.TransactionTypeChoices

CURRENT_URL = f"{BILLING}/subscription/"
PLANS_URL = f"{BILLING}/subscription/eligible-plans/"
RENEW_URL = f"{BILLING}/subscription/renew/"
PREVIEW_URL = f"{BILLING}/subscription/preview-change/"
CHANGE_URL = f"{BILLING}/subscription/change-plan/"
SCHEDULED_URL = f"{BILLING}/subscription/scheduled-change/"
TOP_UP_URL = f"{BILLING}/top-up/"
ADDON_QUOTE_URL = f"{BILLING}/add-ons/quote/"
WALLET_URL = f"{BILLING}/wallet/"


class SubscriptionAPITestCase(TestCase):
    """A tenant on a ₹999 plan, with cheaper and dearer plans to move between."""

    def setUp(self):
        self.ctx = make_tenant("subscription-api")
        self.tenant = self.ctx.tenant
        self.client = self.ctx.client

        self.starter = make_plan(
            "subs-api-starter",
            base_price=Decimal("999.00"),
            tax_percentage=Decimal("18.00"),
            allocated_credits=1000,
            cost_per_additional_credit=Decimal("1.20"),
            sort_order=2,
        )
        self.professional = make_plan(
            "subs-api-professional",
            base_price=Decimal("2699.10"),
            tax_percentage=Decimal("18.00"),
            allocated_credits=3500,
            sort_order=3,
        )
        # Cheap enough that a third of the Starter cycle covers it outright, which is
        # what makes a move to it owe nothing and therefore be scheduled.
        self.basic = make_plan(
            "subs-api-basic",
            base_price=Decimal("200.00"),
            tax_percentage=Decimal("18.00"),
            allocated_credits=200,
            sort_order=1,
        )
        self.yearly = make_plan(
            "subs-api-yearly",
            base_price=Decimal("9990.00"),
            tax_percentage=Decimal("18.00"),
            billing_cycle=Plan.BillingCycleChoices.YEARLY,
            sort_order=4,
        )
        self.free = make_free_plan("subs-api-free", sort_order=0)
        self.retired = make_plan("subs-api-retired", is_active=False, sort_order=9)

        self.subscription = subscription_service.activate(self.tenant, self.starter)
        self.set_cycle(20, 30)

    # --- helpers ------------------------------------------------------------

    def set_cycle(self, days_elapsed, days_total, subscription=None):
        subscription = subscription or self.subscription
        now = timezone.now()
        Subscription.objects.filter(pk=subscription.pk).update(
            start_date=now - timedelta(days=days_elapsed),
            end_date=now + timedelta(days=days_total - days_elapsed, hours=1),
        )
        subscription.refresh_from_db()
        return subscription

    def lapse(self, subscription=None):
        subscription = subscription or self.subscription
        Subscription.objects.filter(pk=subscription.pk).update(
            start_date=timezone.now() - timedelta(days=32),
            end_date=timezone.now() - timedelta(days=2),
        )
        subscription.refresh_from_db()
        subscription_service.expire(subscription)
        subscription.refresh_from_db()
        return subscription

    def plans_by_code(self, client=None):
        response = (client or self.client).get(PLANS_URL)
        return response, {p["code"]: p for p in response.json()["data"]["plans"]}

    @property
    def transactions(self):
        return Transaction.objects.filter(tenant=self.tenant)

    @property
    def wallet(self):
        return Wallet.objects.get(tenant=self.tenant)


class CurrentSubscriptionTests(SubscriptionAPITestCase):
    """BS-01 to BS-08."""

    def test_bs01_reports_the_plan_cycle_credits_and_capacity(self):
        """BS-01: everything a billing screen renders, in one call."""
        response = self.client.get(CURRENT_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()["data"]
        self.assertEqual(data["status"], Status.ACTIVE)
        self.assertEqual(data["plan"]["code"], self.starter.code)
        self.assertEqual(data["credits"]["balance"], 1000)
        self.assertIn("entitlements", data)
        self.assertIn("end_date", data)

    def test_bs02_the_renewal_window_is_reported_not_inferred(self):
        """BS-02: the client must not have to reimplement the window rule, or the
        button appears on a different day from the one the server accepts."""
        self.set_cycle(30 - 20, 30)  # twenty days left
        far = self.client.get(CURRENT_URL).json()["data"]
        self.assertEqual(far["days_remaining"], 20)
        self.assertFalse(far["renewable_now"])

        self.set_cycle(25, 30)  # five days left
        near = self.client.get(CURRENT_URL).json()["data"]
        self.assertEqual(near["days_remaining"], 5)
        self.assertTrue(near["renewable_now"])

    def test_bs03_a_scheduled_change_is_reported_with_its_date(self):
        """BS-03: a tenant who scheduled a downgrade and cannot see it will
        schedule it again."""
        subscription_service.schedule_plan_change(self.subscription, self.basic)

        data = self.client.get(CURRENT_URL).json()["data"]

        self.assertEqual(data["scheduled_change"]["plan_code"], self.basic.code)
        self.assertIsNotNone(data["scheduled_change"]["effective_at"])

    def test_bs04_entitlements_carry_live_usage(self):
        """BS-04: 'you have 2 agents' is what makes a limit actionable."""
        from onboard.tests.billing_factories import make_agent

        make_agent(self.tenant)

        entitlements = {
            row["resource"]: row
            for row in self.client.get(CURRENT_URL).json()["data"]["entitlements"]
        }

        self.assertEqual(entitlements[Resource.AGENT]["used"], 1)
        self.assertEqual(entitlements[Resource.AGENT]["remaining"], 1)

    def test_bs05_the_recurring_add_on_cost_is_reported(self):
        """BS-05: this is what makes the next renewal amount predictable."""
        TenantEntitlement.objects.create(
            tenant=self.tenant, resource=Resource.AGENT,
            additional_quantity=3, price_per_unit=Decimal("500.00"), addon_step=1,
        )

        data = self.client.get(CURRENT_URL).json()["data"]

        self.assertEqual(data["addons_recurring_net"], "1500.00")

    def test_bs06_a_tenant_who_never_subscribed_is_told_so(self):
        """BS-06: a distinct code from the lapsed case — see BS-49."""
        stranger = make_tenant("subs-api-stranger")

        response = stranger.client.get(CURRENT_URL)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("NO_SUBSCRIPTION", response.json()["errors"])

    def test_bs07_anonymous_callers_are_refused(self):
        """BS-07."""
        response = APIClient().get(CURRENT_URL)

        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_bs08_each_tenant_sees_only_its_own_subscription(self):
        """BS-08."""
        other = make_tenant("subs-api-other")
        subscription_service.activate(other, self.professional)

        mine = self.client.get(CURRENT_URL).json()["data"]
        theirs = other.client.get(CURRENT_URL).json()["data"]

        self.assertEqual(mine["plan"]["code"], self.starter.code)
        self.assertEqual(theirs["plan"]["code"], self.professional.code)


class EligiblePlansTests(SubscriptionAPITestCase):
    """BS-09 to BS-14."""

    def test_bs09_every_active_plan_is_listed(self):
        """BS-09: saves the pricing page calling preview-change once per plan."""
        response, plans = self.plans_by_code()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(plans), 5)

    def test_bs10_the_current_plan_is_tagged_rather_than_priced(self):
        """BS-10: quoting a move to the plan you are on is a `PricingError`, so the
        view has to answer this before it asks."""
        _, plans = self.plans_by_code()

        self.assertTrue(plans[self.starter.code]["is_current"])
        self.assertEqual(plans[self.starter.code]["change_type"], "current")
        self.assertEqual(plans[self.starter.code]["amount_due"], "0.00")

    def test_bs11_a_dearer_plan_is_tagged_paid_with_an_amount(self):
        """BS-11."""
        _, plans = self.plans_by_code()

        entry = plans[self.professional.code]
        self.assertEqual(entry["change_type"], pricing.PAID_CHANGE)
        self.assertGreater(Decimal(entry["amount_due"]), Decimal("0.00"))

    def test_bs12_a_plan_that_owes_nothing_is_tagged_scheduled_at_zero(self):
        """BS-12: **never a refund** — the tenant keeps what they paid for until
        the cycle ends."""
        _, plans = self.plans_by_code()

        entry = plans[self.basic.code]
        self.assertEqual(entry["change_type"], pricing.SCHEDULED_CHANGE)
        self.assertEqual(entry["amount_due"], "0.00")
        self.assertIn("effective_at", entry)

    def test_bs13_an_unpriceable_plan_is_still_listed_with_a_reason(self):
        """BS-13: **the tenant should see the plan exists.**

        Dropping it from the list makes a configuration problem look like a plan
        that was withdrawn.
        """
        real_quote = pricing.quote_plan_change

        def sometimes_broken(subscription, plan, **kwargs):
            if plan.pk == self.professional.pk:
                raise pricing.PricingError("This plan is not currently priced.")
            return real_quote(subscription, plan, **kwargs)

        with patch(
            "onboard.views.billing.subscription.pricing.quote_plan_change",
            side_effect=sometimes_broken,
        ):
            _, plans = self.plans_by_code()

        entry = plans[self.professional.code]
        self.assertEqual(entry["change_type"], "unavailable")
        self.assertIn("not currently priced", entry["reason"])

    def test_bs14_inactive_plans_are_not_offered(self):
        """BS-14: a retired plan must not be sellable, however it is reached."""
        _, plans = self.plans_by_code()

        self.assertNotIn(self.retired.code, plans)


class PreviewPlanChangeTests(SubscriptionAPITestCase):
    """BS-15 to BS-19."""

    def preview(self, plan, client=None):
        return (client or self.client).post(
            PREVIEW_URL, {"plan_id": str(plan.pk)}, format="json"
        )

    def test_bs15_a_preview_answers_both_questions(self):
        """BS-15: what it costs **and** what it does to the credits. Either one
        alone leaves the tenant deciding blind."""
        response = self.preview(self.professional)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()["data"]
        self.assertIn("money", data)
        self.assertIn("credits", data)
        self.assertIn("change_type", data["money"])
        self.assertIn("balance_after_change", data["credits"])

    def test_bs16_a_preview_changes_nothing(self):
        """BS-16: no order, no schedule, no plan change."""
        self.preview(self.professional)
        self.preview(self.basic)

        self.assertEqual(self.transactions.count(), 0)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.plan_id, self.starter.pk)
        self.assertIsNone(self.subscription.scheduled_plan_id)

    def test_bs17_a_tenant_about_to_lose_credits_is_warned_first(self):
        """BS-17: **before they click, not after.**"""
        self.starter.allow_rollover_on_upgrade = False
        self.starter.save(update_fields=["allow_rollover_on_upgrade"])
        # The snapshot is the authority, so the live edit has to reach it.
        self.subscription.generate_plan_snapshot()
        self.subscription.save(update_fields=["plan_snapshot"])

        credits = self.preview(self.professional).json()["data"]["credits"]

        self.assertEqual(credits["forfeited"], 1000)
        self.assertTrue(credits["warnings"])
        self.assertIn("1000", credits["warnings"][0])

    def test_bs18_previewing_the_current_plan_is_refused(self):
        """BS-18."""
        response = self.preview(self.starter)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("PLAN_UNCHANGED", response.json()["errors"])

    def test_bs19_an_unknown_plan_is_a_404(self):
        """BS-19: and an *inactive* plan is treated the same way — it is not
        something this tenant may move to."""
        import uuid

        unknown = self.client.post(
            PREVIEW_URL, {"plan_id": str(uuid.uuid4())}, format="json"
        )
        self.assertEqual(unknown.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("PLAN_NOT_FOUND", unknown.json()["errors"])

        self.assertEqual(
            self.preview(self.retired).status_code, status.HTTP_404_NOT_FOUND
        )


class ChangePlanTests(SubscriptionAPITestCase):
    """BS-20 to BS-26."""

    def change(self, plan, client=None, **extra):
        return (client or self.client).post(
            CHANGE_URL, {"plan_id": str(plan.pk), **extra}, format="json"
        )

    def test_bs20_an_upgrade_creates_an_order_and_changes_nothing_yet(self):
        """BS-20: **the plan moves when the money does.**"""
        response = self.change(self.professional)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        transaction = self.transactions.get()
        self.assertEqual(transaction.transaction_type, TxnType.UPGRADE)
        self.assertEqual(transaction.target_plan_id, self.professional.pk)

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.plan_id, self.starter.pk)

    def test_bs21_the_server_prices_the_change_not_the_client(self):
        """BS-21: **a quote is a display artefact; the charge is decided here.**

        A client that could name its own price could upgrade for one rupee.
        """
        honest = pricing.quote_plan_change(self.subscription, self.professional).total

        self.change(self.professional, amount="1.00", total="1.00", net="1.00")

        self.assertEqual(self.transactions.get().amount, honest)

    def test_bs22_a_downgrade_is_scheduled_and_charged_nothing(self):
        """BS-22."""
        response = self.change(self.basic)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()["data"]
        self.assertEqual(body["change_type"], pricing.SCHEDULED_CHANGE)
        self.assertFalse(body["charged"])
        self.assertEqual(self.transactions.count(), 0)

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.plan_id, self.starter.pk)
        self.assertEqual(self.subscription.scheduled_plan_id, self.basic.pk)

    def test_bs23_a_sub_rupee_change_is_applied_without_an_order(self):
        """BS-23: below the gateway minimum, charging is impossible and refusing is
        absurd for a change the tenant asked for."""
        free_tenant = make_tenant("subs-api-sub-rupee")
        subscription = subscription_service.activate(
            free_tenant, make_free_plan("subs-api-sub-rupee-free")
        )
        self.set_cycle(20, 30, subscription)
        nearly_free = make_plan(
            "subs-api-nearly-free",
            base_price=Decimal("0.50"),
            tax_percentage=Decimal("18.00"),
        )

        response = self.change(nearly_free, client=free_tenant.client)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.json()["data"]["change_type"], pricing.IMMEDIATE_FREE
        )
        self.assertFalse(
            Transaction.objects.filter(tenant=free_tenant.tenant).exists()
        )
        self.assertEqual(
            subscription_service.current_for(free_tenant.tenant).plan_id,
            nearly_free.pk,
        )

    def test_bs24_a_change_is_refused_while_an_order_is_open(self):
        """BS-24: paying two upgrade orders and having one applied is the worst
        outcome available."""
        self.client.post(TOP_UP_URL, {"credits": 100}, format="json")

        response = self.change(self.professional)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("PENDING_ORDER_EXISTS", response.json()["errors"])

    def test_bs25_a_scheduled_change_can_be_dropped(self):
        """BS-25."""
        subscription_service.schedule_plan_change(self.subscription, self.basic)

        response = self.client.delete(SCHEDULED_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.subscription.refresh_from_db()
        self.assertIsNone(self.subscription.scheduled_plan_id)
        self.assertIsNone(self.subscription.scheduled_change_at)

    def test_bs26_dropping_nothing_is_refused_rather_than_silently_accepted(self):
        """BS-26: a 200 here would tell a tenant their downgrade was cancelled when
        there was never one to cancel."""
        response = self.client.delete(SCHEDULED_URL)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("NO_SCHEDULED_CHANGE", response.json()["errors"])


class RenewalTests(SubscriptionAPITestCase):
    """BS-27 to BS-32."""

    def test_bs27_renewal_inside_the_window_creates_an_order(self):
        """BS-27."""
        self.set_cycle(25, 30)

        response = self.client.post(RENEW_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        transaction = self.transactions.get()
        self.assertEqual(transaction.transaction_type, TxnType.RENEWAL)

    def test_bs28_renewal_outside_the_window_says_how_long_to_wait(self):
        """BS-28: **long enough to act on a reminder, short enough that nobody pays
        twice for a cycle they are barely into.**"""
        self.set_cycle(0, 30)

        response = self.client.post(RENEW_URL)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("RENEWAL_WINDOW_NOT_OPEN", response.json()["errors"])
        self.assertIn(str(RENEWAL_WINDOW_DAYS), response.json()["message"])
        self.assertIn("30 days", response.json()["message"])

    def test_bs29_the_renewal_price_includes_capacity_still_held(self):
        """BS-29: **a renewal order is the plan's price plus whatever the tenant is
        still renting.** Omitting add-ons undercharges every cycle."""
        self.set_cycle(25, 30)
        TenantEntitlement.objects.create(
            tenant=self.tenant, resource=Resource.AGENT,
            additional_quantity=3, price_per_unit=Decimal("500.00"), addon_step=1,
        )

        self.client.post(RENEW_URL)

        # (999.00 + 1500.00) net, +18%.
        transaction = self.transactions.get()
        self.assertEqual(transaction.amount_breakdown["net"], "2499.00")
        self.assertEqual(transaction.amount, Decimal("2948.82"))

    def test_bs30_a_lapsed_subscription_can_still_be_renewed(self):
        """BS-30: the window is a floor, not a ceiling — being late must never be
        a reason we cannot take the money."""
        self.lapse()

        response = self.client.post(RENEW_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_bs31_renewal_is_refused_while_an_order_is_open(self):
        """BS-31."""
        self.set_cycle(25, 30)
        self.client.post(TOP_UP_URL, {"credits": 100}, format="json")

        response = self.client.post(RENEW_URL)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("PENDING_ORDER_EXISTS", response.json()["errors"])

    def test_bs32_a_cancelled_subscription_is_not_offered_back(self):
        """BS-32: those were superseded by a plan change, so renewing one would
        quote the tenant terms they already moved off."""
        subscription_service.apply_plan_change(self.subscription, self.professional)
        Subscription.objects.filter(
            tenant=self.tenant, status=Status.ACTIVE
        ).update(status=Status.CANCELLED)

        response = self.client.post(RENEW_URL)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("NO_SUBSCRIPTION", response.json()["errors"])


class ExpiredTenantTests(SubscriptionAPITestCase):
    """BS-33 to BS-52 — §13.1.

    **See everything, buy only a plan.** Before this, a lapsed tenant got a 409 from
    every endpoint including the one that would have explained why, and coming back
    on a bigger plan meant renewing the old one and *then* upgrading — two payments
    for one outcome.
    """

    def setUp(self):
        super().setUp()
        wallet_service.grant(
            self.tenant, entry_type=EntryType.MANUAL_TOP_UP, amount=250,
            bucket=Bucket.PURCHASED, reference_id="subs-api-top-up",
        )
        self.lapse()

    def change(self, plan):
        return self.client.post(
            CHANGE_URL, {"plan_id": str(plan.pk)}, format="json"
        )

    # --- reading ------------------------------------------------------------

    def test_bs33_a_lapsed_tenant_can_still_read_their_subscription(self):
        """BS-33: **200, not 409.** The dashboard cannot explain a block it is not
        allowed to see."""
        response = self.client.get(CURRENT_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_bs34_the_response_says_which_plan_lapsed(self):
        """BS-34."""
        data = self.client.get(CURRENT_URL).json()["data"]

        self.assertEqual(data["status"], Status.EXPIRED)
        self.assertEqual(data["plan"]["code"], self.starter.code)

    def test_bs35_their_credits_are_still_visible(self):
        """BS-35: the purchased ones are frozen, not confiscated — and a tenant who
        cannot see them will assume they were taken."""
        data = self.client.get(CURRENT_URL).json()["data"]

        self.assertEqual(data["credits"]["purchased"], 250)
        self.assertTrue(data["credits"]["is_frozen"])

    def test_bs36_renewal_is_reported_as_open(self):
        """BS-36."""
        self.assertTrue(self.client.get(CURRENT_URL).json()["data"]["renewable_now"])

    def test_bs37_days_remaining_floors_at_zero(self):
        """BS-37: "−2 days remaining" is not something a screen can render."""
        self.assertEqual(
            self.client.get(CURRENT_URL).json()["data"]["days_remaining"], 0
        )

    def test_bs38_they_can_see_the_whole_plan_list(self):
        """BS-38."""
        response, plans = self.plans_by_code()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(plans), 5)

    def test_bs39_every_plan_is_offered_at_its_full_price(self):
        """BS-39: **there is no unused time left to credit.**"""
        _, plans = self.plans_by_code()

        self.assertEqual(
            plans[self.professional.code]["amount_due"],
            str(self.professional.total_price),
        )
        self.assertEqual(
            plans[self.basic.code]["amount_due"], str(self.basic.total_price)
        )

    def test_bs40_a_preview_is_marked_as_a_reactivation(self):
        """BS-40: so the client can say "restart on Professional" rather than
        "upgrade", which would be a lie about what is happening."""
        response = self.client.post(
            PREVIEW_URL, {"plan_id": str(self.professional.pk)}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["data"]["money"]["reactivation"], "True")

    # --- coming back --------------------------------------------------------

    def test_bs41_they_can_start_an_order_for_a_different_plan(self):
        """BS-41: **one payment, any plan.**"""
        response = self.change(self.professional)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        transaction = self.transactions.get()
        self.assertEqual(transaction.amount, self.professional.total_price)

    def test_bs42_nothing_changes_until_that_payment_clears(self):
        """BS-42."""
        self.change(self.professional)

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, Status.EXPIRED)
        self.assertEqual(self.subscription.plan_id, self.starter.pk)

    def test_bs43_the_payment_reactivates_them_on_the_new_plan(self):
        """BS-43: old row closed and linked, new row live."""
        order_id = self.change(self.professional).json()["data"]["razorpay_order_id"]

        pay(order_id)

        self.subscription.refresh_from_db()
        live = subscription_service.current_for(self.tenant)
        self.assertIsNotNone(live)
        self.assertEqual(live.plan_id, self.professional.pk)
        self.assertEqual(self.subscription.status, Status.CANCELLED)
        self.assertEqual(self.subscription.superseded_by_id, live.pk)

    def test_bs44_choosing_the_same_plan_points_them_at_renew(self):
        """BS-44: **renew extends the existing row**, so routing this through
        change-plan would open a second subscription for no reason."""
        response = self.change(self.starter)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("PLAN_UNCHANGED", response.json()["errors"])
        self.assertIn("renew", response.json()["message"].lower())

    def test_bs45_renewing_extends_the_existing_row(self):
        """BS-45: one subscription throughout, not a chain of near-identical rows."""
        response = self.client.post(RENEW_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        pay(response.json()["data"]["razorpay_order_id"])

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, Status.ACTIVE)
        self.assertEqual(Subscription.objects.filter(tenant=self.tenant).count(), 1)

    # --- what stays closed --------------------------------------------------

    def test_bs46_they_still_cannot_buy_credits(self):
        """BS-46: **credits belong to a cycle**, and there is not one running."""
        response = self.client.post(TOP_UP_URL, {"credits": 500}, format="json")

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("NO_ACTIVE_SUBSCRIPTION", response.json()["errors"])

    def test_bs47_they_still_cannot_rent_capacity(self):
        """BS-47: renting capacity for a cycle that is not running has no cycle to
        belong to."""
        response = self.client.post(
            ADDON_QUOTE_URL, {"resource": Resource.AGENT, "quantity": 1},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("NO_ACTIVE_SUBSCRIPTION", response.json()["errors"])

    def test_bs48_the_wallet_endpoint_needs_no_subscription_at_all(self):
        """BS-48: the wallet is anchored to the tenant, not the subscription — and
        this is where a lapsed tenant sees their credits are safe."""
        response = self.client.get(WALLET_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["data"]["purchased_balance"], 250)

    # --- neighbouring states ------------------------------------------------

    def test_bs49_never_subscribed_is_a_different_answer_from_lapsed(self):
        """BS-49: the two need different screens — one is "choose a plan", the
        other is "you had Starter, here is what it costs to come back"."""
        stranger = make_tenant("subs-api-never")

        response = stranger.client.get(CURRENT_URL)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("NO_SUBSCRIPTION", response.json()["errors"])
        self.assertNotIn("NO_ACTIVE_SUBSCRIPTION", response.json()["errors"])

    def test_bs50_a_tenant_left_with_only_cancelled_rows_has_no_record(self):
        """BS-50."""
        Subscription.objects.filter(tenant=self.tenant).update(
            status=Status.CANCELLED
        )

        self.assertEqual(
            self.client.get(CURRENT_URL).status_code, status.HTTP_409_CONFLICT
        )

    def test_bs51_none_of_this_changes_anything_for_an_active_tenant(self):
        """BS-51: **the regression guard.** Opening these endpoints to lapsed
        tenants must not loosen them for live ones."""
        live = make_tenant("subs-api-live")
        subscription = subscription_service.activate(live, self.starter)
        self.set_cycle(0, 30, subscription)

        # Renewal is still window-limited.
        self.assertEqual(
            live.client.post(RENEW_URL).status_code, status.HTTP_409_CONFLICT
        )
        # A downgrade is still scheduled rather than sold at full price.
        downgrade = live.client.post(
            CHANGE_URL, {"plan_id": str(self.basic.pk)}, format="json"
        ).json()["data"]
        self.assertEqual(downgrade["change_type"], pricing.SCHEDULED_CHANGE)
        self.assertFalse(downgrade["charged"])

    def test_bs52_a_second_change_after_reactivating_is_an_ordinary_one(self):
        """BS-52: once they are live again the reactivation path is behind them —
        the next move prorates like anybody else's."""
        order_id = self.change(self.professional).json()["data"]["razorpay_order_id"]
        pay(order_id)
        live = subscription_service.current_for(self.tenant)
        self.set_cycle(20, 30, live)

        quote = pricing.quote_plan_change(live, self.starter)

        self.assertFalse(quote.detail["reactivation"])
        self.assertGreater(
            Decimal(str(quote.detail["credit_from_current"])), Decimal("0.00")
        )
