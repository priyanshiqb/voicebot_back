"""Plan v2 — the whole lifecycle of a minutes-based subscription.

The other minutes suites test pieces: pricing a quantity, matching a band, quoting a
change. This one walks the transitions a real tenant goes through, and in particular
the ones where **money has already changed hands** — an upgrade order that gets paid, a
renewal that gets paid, an expiry, a reactivation.

Quoting a change correctly and *applying* it correctly are different failures. A quote
that is wrong shows somebody a bad number; an application that is wrong takes their
money and gives them the wrong thing.

Run with ``--settings=config.settings.test_postgres``.
"""

from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from onboard.models import Subscription, Transaction
from onboard.services import pricing
from onboard.services import subscriptions as subscription_service
from onboard.tests.billing_factories import (
    BILLING,
    DEFAULT_RATES,
    Operation,
    add_rates,
    age_subscription,
    expire_subscription,
    make_minutes_plan,
    make_plan,
    make_tenant,
    pay,
    subscribe,
)
from wallet.models import CreditLedger
from wallet.services import wallet as wallet_service

HIGH = Operation.CONVERSATION_HIGH_LATENCY

TxnType = Transaction.TransactionTypeChoices


class MinutesLifecycleBase(APITestCase):

    def setUp(self):
        self.ctx = make_tenant()
        self.client = self.ctx.client
        self.tenant = self.ctx.tenant
        self.plan = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
            discount_percentage=Decimal("0.00"), tax_percentage=Decimal("18.00"),
            allow_rollover_on_upgrade=True, allow_rollover_on_renewal=False,
            # The factory caps rollover at 0 by default, which would forfeit
            # everything regardless of the flags. Uncapped is the realistic shape.
            max_rollover_credits=None,
            cost_per_additional_credit=Decimal("1.00"),
            sort_order=1,
        )
        self.subscription = subscribe(self.tenant, self.plan, minutes=1000)

    def live(self):
        return subscription_service.current_for(self.tenant)


class UpgradeAppliesTests(MinutesLifecycleBase):
    """Money changed hands. The tenant must get the quantity they paid for."""

    def setUp(self):
        super().setUp()
        age_subscription(self.subscription, days_in=10)

    def _order_upgrade(self, minutes):
        response = self.client.post(
            f"{BILLING}/subscription/change-plan/",
            {"plan_id": str(self.plan.pk), "minutes": minutes},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]["razorpay_order_id"]

    def test_paying_an_upgrade_moves_the_tenant_to_the_paid_quantity(self):
        order_id = self._order_upgrade(2500)

        pay(order_id, client=self.client)

        live = self.live()
        self.assertEqual(live.selected_minutes, 2500)
        self.assertEqual(live.allocated_credits, 2500 * DEFAULT_RATES[HIGH])
        self.assertEqual(live.plan_snapshot["selected_minutes"], 2500)

    def test_the_quantity_comes_from_the_order_not_from_a_recomputation(self):
        """The band can move between the order and the payment. The tenant paid for a
        specific quantity and must receive that one."""
        order_id = self._order_upgrade(2500)
        self.plan.min_minutes = 5000
        self.plan.max_minutes = 9000
        self.plan.save(update_fields=["min_minutes", "max_minutes"])

        pay(order_id, client=self.client)

        self.assertEqual(self.live().selected_minutes, 2500)

    def test_the_grant_comes_from_the_rate_frozen_on_the_order(self):
        order_id = self._order_upgrade(2500)
        old = self.plan.basis_rate()
        old.effective_to = timezone.now()
        old.save(update_fields=["effective_to"])
        add_rates(self.plan, {HIGH: (99, Decimal("99.00"))})

        pay(order_id, client=self.client)

        live = self.live()
        wallet = wallet_service.for_tenant(self.tenant)
        # Whatever the grant is, the column and the ledger must agree on it.
        granted = sum(
            e.amount for e in CreditLedger.objects.filter(
                wallet=wallet, entry_type=CreditLedger.EntryType.CYCLE_ALLOCATION,
                subscription=live,
            )
        )
        self.assertEqual(granted, live.allocated_credits)

    def test_the_old_subscription_is_superseded_not_left_live(self):
        order_id = self._order_upgrade(2500)

        pay(order_id, client=self.client)

        self.assertEqual(
            Subscription.objects.filter(tenant=self.tenant, status="active").count(), 1
        )
        previous = Subscription.objects.get(pk=self.subscription.pk)
        self.assertEqual(previous.status, "cancelled")
        self.assertEqual(previous.superseded_by_id, self.live().pk)

    def test_an_upgrade_across_plans_carries_the_requested_quantity(self):
        target = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
            minute_prices={HIGH: Decimal("3.60")},
            discount_percentage=Decimal("0.00"), sort_order=2,
        )
        response = self.client.post(
            f"{BILLING}/subscription/change-plan/",
            {"plan_id": str(target.pk), "minutes": 1500},
            format="json",
        )
        order_id = response.data["data"]["razorpay_order_id"]

        pay(order_id, client=self.client)

        live = self.live()
        self.assertEqual(live.plan_id, target.pk)
        self.assertEqual(live.selected_minutes, 1500)

    def test_upgrading_onto_a_pack_lands_on_the_packs_quantity(self):
        pack = make_minutes_plan(
            min_minutes=5000, max_minutes=5000, credit_basis=HIGH,
            discount_percentage=Decimal("0.00"), sort_order=2,
        )
        response = self.client.post(
            f"{BILLING}/subscription/change-plan/",
            {"plan_id": str(pack.pk)},
            format="json",
        )
        order_id = response.data["data"]["razorpay_order_id"]

        pay(order_id, client=self.client)

        self.assertEqual(self.live().selected_minutes, 5000)


class RenewalAppliesTests(MinutesLifecycleBase):

    def setUp(self):
        super().setUp()
        age_subscription(self.subscription, days_in=25)

    def test_paying_a_renewal_extends_the_same_row_at_the_same_quantity(self):
        order_id = self.client.post(
            f"{BILLING}/subscription/renew/", format="json"
        ).data["data"]["razorpay_order_id"]
        before_end = Subscription.objects.get(pk=self.subscription.pk).end_date

        pay(order_id, client=self.client)

        renewed = Subscription.objects.get(pk=self.subscription.pk)
        self.assertGreater(renewed.end_date, before_end)
        self.assertEqual(renewed.selected_minutes, 1000)
        self.assertEqual(renewed.allocated_credits, 1000 * DEFAULT_RATES[HIGH])

    def test_a_renewal_re_derives_the_grant_from_todays_rate(self):
        """A renewal is a future cycle — the one place a rate change legitimately
        reaches an existing customer."""
        old = self.plan.basis_rate()
        old.effective_to = timezone.now()
        old.save(update_fields=["effective_to"])
        add_rates(self.plan, {HIGH: (12, Decimal("2.40"))})

        subscription_service.renew(
            Subscription.objects.get(pk=self.subscription.pk)
        )

        renewed = Subscription.objects.get(pk=self.subscription.pk)
        self.assertEqual(renewed.allocated_credits, 1000 * 12)
        self.assertEqual(renewed.plan_snapshot["credits_per_minute"], 12)
        self.assertEqual(renewed.plan_snapshot["price_per_minute_inr"], "2.4000")

    def test_a_lapsed_minutes_subscription_can_be_renewed(self):
        expire_subscription(self.subscription)

        subscription_service.renew(
            Subscription.objects.get(pk=self.subscription.pk)
        )

        renewed = Subscription.objects.get(pk=self.subscription.pk)
        self.assertEqual(renewed.status, "active")
        self.assertEqual(renewed.selected_minutes, 1000)
        self.assertFalse(wallet_service.for_tenant(self.tenant).is_frozen)


class ExpiryAndReactivationTests(MinutesLifecycleBase):

    def test_a_minutes_subscription_expires_and_freezes_the_wallet(self):
        subscription_service.expire(
            Subscription.objects.get(pk=self.subscription.pk)
        )

        expired = Subscription.objects.get(pk=self.subscription.pk)
        wallet = wallet_service.for_tenant(self.tenant)
        self.assertEqual(expired.status, "expired")
        self.assertTrue(wallet.is_frozen)
        self.assertEqual(wallet.allocated_balance, 0)

    def test_reactivating_from_expiry_is_priced_at_full_price(self):
        """A lapsed subscription has no unused time to credit, so this stops being an
        upgrade-or-downgrade question."""
        expire_subscription(self.subscription)
        expired = Subscription.objects.get(pk=self.subscription.pk)

        quote = pricing.quote_plan_change(expired, self.plan, minutes=500)

        self.assertTrue(quote.detail["reactivation"])
        self.assertEqual(quote.detail["selected_minutes"], 500)
        self.assertEqual(quote.net, Decimal("500") * Decimal("1.80"))

    def test_a_smaller_quantity_on_a_lapsed_plan_still_charges(self):
        """Not scheduled — there is no paid cycle left to schedule against."""
        expire_subscription(self.subscription)
        expired = Subscription.objects.get(pk=self.subscription.pk)

        quote = pricing.quote_plan_change(expired, self.plan, minutes=100)

        self.assertNotEqual(quote.detail["change_type"], pricing.SCHEDULED_CHANGE)
        self.assertGreater(quote.total, Decimal("0.00"))


class RolloverTests(MinutesLifecycleBase):
    """What happens to credits when the quantity changes."""

    def test_an_upgrade_carries_credits_when_the_plan_allows_it(self):
        age_subscription(self.subscription, days_in=10)
        before = wallet_service.for_tenant(self.tenant).balance
        self.assertEqual(before, 1000 * DEFAULT_RATES[HIGH])

        subscription_service.apply_plan_change(
            Subscription.objects.get(pk=self.subscription.pk),
            self.plan, minutes=2000, reason="upgrade",
        )

        after = wallet_service.for_tenant(self.tenant).balance
        # Rollover is on for upgrades, so the old balance survives and the new
        # allocation is added on top.
        self.assertEqual(after, before + 2000 * DEFAULT_RATES[HIGH])

    def test_a_downgrade_forfeits_when_the_plan_says_so(self):
        plan = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
            allow_rollover_on_downgrade=False, sort_order=3,
        )
        ctx = make_tenant()
        subscription = subscribe(ctx.tenant, plan, minutes=2000)
        age_subscription(subscription, days_in=10)

        subscription_service.apply_plan_change(
            Subscription.objects.get(pk=subscription.pk),
            plan, minutes=500, reason="downgrade",
        )

        wallet = wallet_service.for_tenant(ctx.tenant)
        # The old allocation is burned; only the new one remains.
        self.assertEqual(wallet.allocated_balance, 500 * DEFAULT_RATES[HIGH])


class TopUpAndAddonTests(MinutesLifecycleBase):
    """Both are priced off the subscription, so both have to work on a v2 plan."""

    def test_a_top_up_can_be_quoted_on_a_minutes_subscription(self):
        response = self.client.post(
            f"{BILLING}/top-up/quote/", {"credits": 500}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["net"], "500.00")

    def test_an_addon_can_be_quoted_on_a_minutes_subscription(self):
        age_subscription(self.subscription, days_in=10)

        response = self.client.post(
            f"{BILLING}/add-ons/quote/",
            {"resource": "agent", "quantity": 1},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)

    def test_a_renewal_order_includes_held_addons(self):
        from onboard.services import entitlements as entitlement_service

        entitlement_service.apply_addon(
            tenant_id=self.tenant.id, subscription=self.subscription,
            resource="agent", additional_units=2,
        )
        age_subscription(self.subscription, days_in=25)

        response = self.client.post(f"{BILLING}/subscription/renew/", format="json")

        plan_net = pricing.net_for(self.plan, 1000)
        addons = entitlement_service.recurring_total(self.tenant.id)
        expected = (plan_net + addons) * Decimal("1.18")
        self.assertEqual(
            Decimal(response.data["data"]["amount_display"]),
            expected.quantize(Decimal("0.01")),
        )


class UnsellablePlanTests(APITestCase):
    """What is left of the legacy model after Phases 8 and 9.

    This class used to assert that both generations of plan could be sold side by
    side, which was the point of the migration. The migration is over: `base_price`
    and `allocated_credits` are gone, so a plan with no basis tier has nothing to be
    priced or granted from and the activation gate refuses it.

    What remains is the historical case — a row that was active before, or one whose
    basis was removed underneath it. It must degrade quietly rather than crash.
    """

    def setUp(self):
        self.ctx = make_tenant()
        self.client = self.ctx.client
        self.stranded = make_plan(credit_basis=None, sort_order=1)
        self.v2 = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
            discount_percentage=Decimal("0.00"), sort_order=2,
        )

    def test_a_plan_without_a_basis_prices_and_grants_nothing(self):
        self.assertEqual(self.stranded.price_components(1000).total, Decimal("0.00"))
        self.assertEqual(self.stranded.credits_for(1000), 0)

    def test_a_subscription_to_one_still_saves_and_reads_back(self):
        """No price and no credits, but no exception either."""
        subscription = subscribe(self.ctx.tenant, self.stranded)

        self.assertEqual(subscription.plan_snapshot["total_price"], "0.00")
        self.assertEqual(subscription.rules["allocated_credits"], 0)
        self.assertIsNone(subscription.selected_minutes)

    def test_such_a_tenant_can_still_move_to_a_real_plan(self):
        """The way out. Somebody stranded on one must be able to buy their way off
        it, so this path has to keep working."""
        subscription = subscribe(self.ctx.tenant, self.stranded)
        age_subscription(subscription, days_in=10)

        response = self.client.post(
            f"{BILLING}/subscription/change-plan/",
            {"plan_id": str(self.v2.pk), "minutes": 2000},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        pay(response.data["data"]["razorpay_order_id"], client=self.client)

        live = subscription_service.current_for(self.ctx.tenant)
        self.assertEqual(live.plan_id, self.v2.pk)
        self.assertEqual(live.selected_minutes, 2000)
        self.assertEqual(live.allocated_credits, 2000 * DEFAULT_RATES[HIGH])

    def test_renewing_one_repeats_what_it_last_granted(self):
        """There is no plan number left to re-derive from, so the subscription's own
        record is the only honest answer."""
        subscription = subscribe(self.ctx.tenant, self.stranded)
        Subscription.objects.filter(pk=subscription.pk).update(allocated_credits=250)
        age_subscription(subscription, days_in=25)

        subscription_service.renew(Subscription.objects.get(pk=subscription.pk))

        renewed = Subscription.objects.get(pk=subscription.pk)
        self.assertEqual(renewed.allocated_credits, 250)
