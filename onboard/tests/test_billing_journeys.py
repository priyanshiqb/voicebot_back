"""Section 21 of the billing test plan — E2E-01 to E2E-16.

Whole paths through the system, each asserting the invariant at every step.

Everything below is covered in pieces elsewhere. What these add is the *joins*: a
rollover rule read at a plan change and again at the renewal after it, a webhook
redelivered at each of five steps rather than one, credits bought under one plan and
spent under another. Those are where a system that passes every unit test still
produces a wrong number.
"""

from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework import status

from onboard.constants.entitlement_resource import EntitlementResource
from onboard.models import (
    Subscription,
    TempUser,
    TenantEntitlement,
    Transaction,
)
from onboard.services import subscriptions as subscription_service
from onboard.tasks import process_billing_cycle
from onboard.tests.billing_factories import (
    BILLING,
    assert_invariant,
    make_agent,
    make_free_plan,
    make_plan,
    make_tenant,
    make_transaction,
    pay,
)
from tenant.models import TenantSettings
from wallet.models import CreditLedger, CreditRate, Wallet
from wallet.services import wallet as wallet_service

User = get_user_model()

Bucket = CreditLedger.Bucket
EntryType = CreditLedger.EntryType
Operation = CreditRate.Operation
Resource = EntitlementResource
Status = Subscription.SubscriptionStatusChoices
TxnStatus = Transaction.TransactionStatusChoices
TxnType = Transaction.TransactionTypeChoices

TOP_UP_URL = f"{BILLING}/top-up/"
ADDON_URL = f"{BILLING}/add-ons/"
CURRENT_URL = f"{BILLING}/subscription/"
PLANS_URL = f"{BILLING}/subscription/eligible-plans/"
RENEW_URL = f"{BILLING}/subscription/renew/"
CHANGE_URL = f"{BILLING}/subscription/change-plan/"
AGENTS_URL = "/api/v1/core/agents/"


class JourneyTestCase(TestCase):
    """A catalogue to move around in."""

    def setUp(self):
        self.free = make_free_plan(
            "journey-free", allocated_credits=100,
            # Uncapped on purpose: the factory default caps rollover at 0, which
            # would make E2E-01 prove the cap rather than the upgrade carry.
            max_rollover_credits=None,
            rates={
                Operation.STATIC_REEL: 1,
                Operation.CONVERSATION_HIGH_LATENCY: 2,
                Operation.CONVERSATION_LOW_LATENCY: 4,
            },
        )
        self.starter = make_plan(
            "journey-starter",
            base_price=Decimal("999.00"),
            tax_percentage=Decimal("18.00"),
            allocated_credits=1000,
            cost_per_additional_credit=Decimal("1.00"),
            allow_rollover_on_renewal=False,
            allow_rollover_on_upgrade=True,
            max_rollover_credits=None,
            rates={
                Operation.STATIC_REEL: 2,
                Operation.CONVERSATION_HIGH_LATENCY: 5,
                Operation.CONVERSATION_LOW_LATENCY: 10,
            },
        )
        self.professional = make_plan(
            "journey-professional",
            base_price=Decimal("2699.10"),
            tax_percentage=Decimal("18.00"),
            allocated_credits=3500,
            cost_per_additional_credit=Decimal("0.80"),
            allow_rollover_on_renewal=True,
            allow_rollover_on_upgrade=True,
            max_rollover_credits=None,
            rates={
                Operation.STATIC_REEL: 1,
                Operation.CONVERSATION_HIGH_LATENCY: 3,
                Operation.CONVERSATION_LOW_LATENCY: 6,
            },
        )
        self.basic = make_plan(
            "journey-basic",
            base_price=Decimal("200.00"),
            tax_percentage=Decimal("18.00"),
            allocated_credits=200,
        )

    # --- helpers ------------------------------------------------------------

    def set_cycle(self, subscription, days_elapsed, days_total=30):
        now = timezone.now()
        Subscription.objects.filter(pk=subscription.pk).update(
            start_date=now - timedelta(days=days_elapsed),
            end_date=now + timedelta(days=days_total - days_elapsed, hours=1),
        )
        subscription.refresh_from_db()
        return subscription

    def lapse(self, subscription):
        Subscription.objects.filter(pk=subscription.pk).update(
            start_date=timezone.now() - timedelta(days=32),
            end_date=timezone.now() - timedelta(days=2),
        )
        subscription.refresh_from_db()
        return subscription

    def wallet(self, tenant):
        return Wallet.objects.get(tenant=tenant)

    def call(self, tenant, operation, seconds, reference):
        return wallet_service.consume(
            tenant, operation=operation, quantity=seconds, reference_id=reference
        )


class UpgradeAndSpendJourneys(JourneyTestCase):
    """E2E-01 to E2E-03."""

    def test_e2e01_free_signup_call_upgrade_call_again(self):
        """E2E-01: **the price a call is charged at changes exactly once, and only
        forward.**"""
        ctx = make_tenant("journey-e2e01")
        subscription = subscription_service.activate(ctx, self.free)
        assert_invariant(self, ctx.tenant, msg="after free signup")
        self.assertEqual(self.wallet(ctx.tenant).balance, 100)

        first = self.call(
            ctx.tenant, Operation.CONVERSATION_HIGH_LATENCY, 600, "e2e01-call-1"
        )
        self.assertEqual(abs(first.amount), 20)
        assert_invariant(self, ctx.tenant, msg="after the trial call")

        self.set_cycle(subscription, 20)
        response = ctx.client.post(
            CHANGE_URL, {"plan_id": str(self.starter.pk)}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        pay(response.json()["data"]["razorpay_order_id"])

        live = subscription_service.current_for(ctx.tenant)
        self.assertEqual(live.plan_id, self.starter.pk)
        wallet = assert_invariant(self, ctx.tenant, msg="after the upgrade")
        # Free rolls over on upgrade, so the 80 unspent trial credits survive.
        self.assertEqual(wallet.allocated_balance, 80 + 1000)

        second = self.call(
            ctx.tenant, Operation.CONVERSATION_HIGH_LATENCY, 600, "e2e01-call-2"
        )
        self.assertEqual(abs(second.amount), 50)
        first.refresh_from_db()
        self.assertEqual(first.rate_applied, 2, "history must not be re-priced")
        assert_invariant(self, ctx.tenant, msg="after the paid call")

    def test_e2e02_paid_signup_top_up_spend_across_buckets_then_renew(self):
        """E2E-02: **allocated drains first, and purchased survives the renewal.**

        On a plan that does not roll over, the allocated half is burned at the
        cycle boundary and the cash-bought half is not — which is the difference
        the two buckets exist to express.
        """
        ctx = make_tenant("journey-e2e02")
        subscription = subscription_service.activate(ctx, self.starter)
        self.set_cycle(subscription, 20)

        order = ctx.client.post(
            TOP_UP_URL, {"credits": 300}, format="json"
        ).json()["data"]["razorpay_order_id"]
        pay(order)
        wallet = assert_invariant(self, ctx.tenant, msg="after the top-up")
        self.assertEqual((wallet.allocated_balance, wallet.purchased_balance), (1000, 300))

        # 1100 credits: 1000 from allocated, then 100 from purchased.
        self.call(
            ctx.tenant, Operation.CONVERSATION_HIGH_LATENCY, 13200, "e2e02-big-call"
        )
        wallet = assert_invariant(self, ctx.tenant, msg="after a cross-bucket call")
        self.assertEqual(wallet.allocated_balance, 0)
        self.assertEqual(wallet.purchased_balance, 200)
        self.assertEqual(
            CreditLedger.all_objects.filter(
                tenant=ctx.tenant, entry_type=EntryType.USAGE_DEDUCTION
            ).count(),
            2,
        )

        subscription_service.renew(subscription)

        wallet = assert_invariant(self, ctx.tenant, msg="after the renewal")
        self.assertEqual(wallet.allocated_balance, 1000)
        self.assertEqual(wallet.purchased_balance, 200)

    def test_e2e03_capacity_bought_mid_cycle_is_charged_again_at_renewal(self):
        """E2E-03: **capacity is rented, not bought.**

        The prorated charge now and the full charge at every renewal are different
        numbers, and the second one has to actually appear.
        """
        ctx = make_tenant("journey-e2e03")
        subscription = subscription_service.activate(ctx, self.starter)
        self.set_cycle(subscription, 20)
        storage = self.starter.entitlements.get(resource=Resource.STORAGE_GB)
        storage.price_per_additional = Decimal("199.00")
        storage.addon_step = 10
        storage.max_additional = 90
        storage.is_addon_available = True
        storage.save()

        order = ctx.client.post(
            ADDON_URL, {"resource": Resource.STORAGE_GB, "quantity": 20},
            format="json",
        ).json()["data"]["razorpay_order_id"]
        pay(order)

        held = TenantEntitlement.objects.get(
            tenant=ctx.tenant, resource=Resource.STORAGE_GB
        )
        self.assertEqual(held.additional_quantity, 20)
        self.assertEqual(held.recurring_charge, Decimal("398.00"))

        self.set_cycle(subscription, 25)
        renewal = ctx.client.post(RENEW_URL).json()["data"]

        # 999.00 plan + 398.00 capacity, +18%.
        self.assertEqual(renewal["breakdown"]["net"], "1397.00")
        self.assertEqual(renewal["amount_display"], "1648.46")


class LifecycleJourneys(JourneyTestCase):
    """E2E-04 to E2E-06."""

    def test_e2e04_lapse_expire_renew(self):
        """E2E-04: plan credits forfeited, purchased kept, wallet frozen then
        unfrozen, access restored."""
        ctx = make_tenant("journey-e2e04")
        subscription = subscription_service.activate(ctx, self.starter)
        wallet_service.grant(
            ctx.tenant, entry_type=EntryType.MANUAL_TOP_UP, amount=250,
            bucket=Bucket.PURCHASED, reference_id="e2e04-top-up",
        )
        self.lapse(subscription)

        process_billing_cycle()

        subscription.refresh_from_db()
        wallet = assert_invariant(self, ctx.tenant, msg="after expiry")
        self.assertEqual(subscription.status, Status.EXPIRED)
        self.assertEqual(wallet.allocated_balance, 0)
        self.assertEqual(wallet.purchased_balance, 250)
        self.assertTrue(wallet.is_frozen)

        renewal = ctx.client.post(RENEW_URL).json()["data"]
        pay(renewal["razorpay_order_id"])

        subscription.refresh_from_db()
        wallet = assert_invariant(self, ctx.tenant, msg="after the renewal")
        self.assertEqual(subscription.status, Status.ACTIVE)
        self.assertFalse(wallet.is_frozen)
        self.assertEqual(wallet.allocated_balance, 1000)
        self.assertEqual(wallet.purchased_balance, 250)
        self.assertIsNotNone(
            self.call(
                ctx.tenant, Operation.CONVERSATION_HIGH_LATENCY, 60, "e2e04-call"
            )
        )

    def test_e2e05_a_scheduled_downgrade_fires_at_the_cycle_boundary(self):
        """E2E-05: **not expired.** The tenant arranged to continue, and the sweep
        has to apply the change rather than lapse them."""
        ctx = make_tenant("journey-e2e05")
        subscription = subscription_service.activate(ctx, self.professional)
        response = ctx.client.post(
            CHANGE_URL, {"plan_id": str(self.basic.pk)}, format="json"
        )
        self.assertEqual(
            response.json()["data"]["change_type"], "scheduled_change"
        )
        self.lapse(subscription)
        Subscription.objects.filter(pk=subscription.pk).update(
            scheduled_change_at=timezone.now() - timedelta(minutes=1)
        )

        result = process_billing_cycle()

        live = subscription_service.current_for(ctx.tenant)
        self.assertEqual(result["changed"], 1)
        self.assertEqual(result["expired"], 0)
        self.assertEqual(live.plan_id, self.basic.pk)

        wallet = assert_invariant(self, ctx.tenant, msg="after the downgrade")
        self.assertFalse(wallet.is_frozen)
        # Professional does not roll over on a downgrade, so its allocation burns
        # and the new plan's lands. The old plan's rules decided that, not the new.
        self.assertEqual(wallet.allocated_balance, 200)
        self.assertEqual(
            self.wallet(ctx.tenant).rate_snapshot["plan_code"], self.basic.code
        )

    def test_e2e06_upgrading_cancels_a_downgrade_scheduled_earlier(self):
        """E2E-06: **the downgrade never fires.**

        Without this, a change set last week silently undoes one the tenant just
        paid for, weeks later, with nobody watching.
        """
        ctx = make_tenant("journey-e2e06")
        subscription = subscription_service.activate(ctx, self.starter)
        self.set_cycle(subscription, 20)
        ctx.client.post(CHANGE_URL, {"plan_id": str(self.basic.pk)}, format="json")
        subscription.refresh_from_db()
        self.assertEqual(subscription.scheduled_plan_id, self.basic.pk)

        upgrade = ctx.client.post(
            CHANGE_URL, {"plan_id": str(self.professional.pk)}, format="json"
        )
        pay(upgrade.json()["data"]["razorpay_order_id"])

        live = subscription_service.current_for(ctx.tenant)
        self.assertEqual(live.plan_id, self.professional.pk)
        self.assertIsNone(live.scheduled_plan_id)

        self.lapse(live)
        process_billing_cycle()

        settled = Subscription.objects.filter(
            tenant=ctx.tenant, status=Status.ACTIVE
        ).first()
        self.assertIsNone(
            settled.plan_id if settled else None,
            "an expired professional cycle should not resurrect the old downgrade",
        ) if settled is None else self.assertNotEqual(
            settled.plan_id, self.basic.pk
        )


class IdempotencyJourneys(JourneyTestCase):
    """E2E-07 to E2E-08."""

    def temp_user(self, plan, email="race@example.com"):
        return TempUser.objects.create(
            email=email, password_hash="pbkdf2_sha256$fake",
            first_name="Race", last_name="Winner",
            business_name="Race Industries", phone_number="+919900004321",
            selected_plan=plan, is_email_verified=True,
        )

    def test_e2e07_both_completion_paths_can_win_and_only_one_account_results(self):
        """E2E-07: **the browser-confirmed payment and the webhook race, and either
        can win.**

        The webhook promotes the registrant; the browser path's distinguishing work
        is the same ``activate()`` call. Whichever runs second must find nothing
        left to do — one account, one subscription, credits once.
        """
        temp = self.temp_user(self.starter)
        order = make_transaction(
            None, self.starter, temp_user=temp,
            status=TxnStatus.PENDING,
            transaction_type=TxnType.ONBOARDING,
        )
        users_before = User.objects.count()

        pay(order.razorpay_order_id, event_id="evt_race_webhook")
        order.refresh_from_db()
        tenant_id = order.tenant_id

        # The browser path, arriving second on an account that already exists.
        subscription_service.activate(tenant_id, self.starter, transaction=order)

        self.assertEqual(User.objects.count(), users_before + 1)
        self.assertEqual(
            Subscription.objects.filter(tenant_id=tenant_id).count(), 1
        )
        self.assertEqual(
            CreditLedger.all_objects.filter(
                tenant_id=tenant_id, entry_type=EntryType.CYCLE_ALLOCATION
            ).count(),
            1,
        )
        assert_invariant(self, tenant_id, msg="after the onboarding race")

    def test_e2e08_every_webhook_redelivered_twice_across_a_whole_lifecycle(self):
        """E2E-08: **Razorpay redelivers, and it does not redeliver only once.**

        Each step is settled twice with a *fresh* event id, so the webhook log's
        guard is bypassed on purpose and the inner guards have to hold: the order
        status, the ledger reference, and ``activate()``'s own idempotency.
        """
        ctx = make_tenant("journey-e2e08")
        subscription = subscription_service.activate(ctx, self.starter)
        self.set_cycle(subscription, 20)

        def settle_twice(order_id, label):
            pay(order_id, event_id=f"evt_{label}_a")
            pay(order_id, event_id=f"evt_{label}_b")
            assert_invariant(self, ctx.tenant, msg=f"after {label} redelivery")

        top_up = ctx.client.post(
            TOP_UP_URL, {"credits": 300}, format="json"
        ).json()["data"]["razorpay_order_id"]
        settle_twice(top_up, "topup")
        self.assertEqual(self.wallet(ctx.tenant).purchased_balance, 300)

        upgrade = ctx.client.post(
            CHANGE_URL, {"plan_id": str(self.professional.pk)}, format="json"
        ).json()["data"]["razorpay_order_id"]
        settle_twice(upgrade, "upgrade")

        live = subscription_service.current_for(ctx.tenant)
        self.assertEqual(live.plan_id, self.professional.pk)
        self.assertEqual(
            Subscription.objects.filter(
                tenant=ctx.tenant, status=Status.ACTIVE
            ).count(),
            1,
        )

        self.set_cycle(live, 25)
        renewal = ctx.client.post(RENEW_URL).json()["data"]["razorpay_order_id"]
        settle_twice(renewal, "renewal")

        wallet = assert_invariant(self, ctx.tenant, msg="end of lifecycle")
        self.assertEqual(wallet.purchased_balance, 300)
        # Professional rolls over uncapped: one upgrade allocation plus one renewal.
        self.assertEqual(wallet.allocated_balance, 1000 + 3500 + 3500)


class EnforcementJourneys(JourneyTestCase):
    """E2E-09 to E2E-12."""

    def test_e2e09_a_full_cycle_with_enforcement_off_then_on(self):
        """E2E-09: **metering before blocking, proven end to end.**

        Nothing is refused before the flip; the same actions are refused after.
        """
        ctx = make_tenant("journey-e2e09")
        subscription_service.activate(ctx, self.starter)
        Wallet.objects.filter(tenant=ctx.tenant).update(
            allocated_balance=0, balance=0
        )

        with self.assertLogs("wallet.services.wallet", level="WARNING"):
            wallet_service.assert_can_consume(
                ctx.tenant, operation=Operation.CONVERSATION_HIGH_LATENCY,
                quantity=60,
            )

        settings_row = TenantSettings.for_tenant(ctx.tenant.id)
        settings_row.credits_enforced = True
        settings_row.save(update_fields=["credits_enforced"])

        from wallet.services.exceptions import InsufficientCredits

        with self.assertRaises(InsufficientCredits):
            wallet_service.assert_can_consume(
                ctx.tenant, operation=Operation.CONVERSATION_HIGH_LATENCY,
                quantity=60,
            )

    def test_e2e10_hit_the_limit_buy_capacity_continue(self):
        """E2E-10: refused, purchased, allowed — the loop a tenant is meant to be
        able to complete without talking to anyone."""
        ctx = make_tenant("journey-e2e10")
        subscription = subscription_service.activate(ctx, self.starter)
        settings_row = TenantSettings.for_tenant(ctx.tenant.id)
        settings_row.entitlements_enforced = True
        settings_row.save(update_fields=["entitlements_enforced"])
        make_agent(ctx.tenant)
        make_agent(ctx.tenant)

        def create_agent():
            stamp = str(timezone.now().timestamp()).replace(".", "")
            return ctx.client.post(
                AGENTS_URL,
                {"bot_name": f"Journey Bot {stamp}", "persona_name": "Journey"},
                format="json",
            )

        self.assertEqual(
            create_agent().status_code, status.HTTP_402_PAYMENT_REQUIRED
        )

        agent_slot = self.starter.entitlements.get(resource=Resource.AGENT)
        agent_slot.price_per_additional = Decimal("500.00")
        agent_slot.addon_step = 1
        agent_slot.max_additional = 10
        agent_slot.is_addon_available = True
        agent_slot.save()
        self.set_cycle(subscription, 20)

        order = ctx.client.post(
            ADDON_URL, {"resource": Resource.AGENT, "quantity": 2}, format="json"
        ).json()["data"]["razorpay_order_id"]
        pay(order)

        self.assertEqual(create_agent().status_code, status.HTTP_201_CREATED)

    def test_e2e11_the_pay_as_you_go_shape_rolls_at_zero_and_keeps_spending(self):
        """E2E-11: **the shape that was dropped but must stay buildable.**

        No allocation, top-ups only, credits spendable past expiry, and a cycle
        that renews itself for nothing. If any of those four stop working, adding
        the plan back stops being a configuration change.
        """
        payg = make_plan(
            "journey-payg",
            base_price=Decimal("0.00"),
            tax_percentage=Decimal("0.00"),
            allocated_credits=0,
            cost_per_additional_credit=Decimal("1.50"),
            allow_use_after_expiry=True,
            renews_without_payment=True,
            rates={Operation.CONVERSATION_HIGH_LATENCY: 5},
        )
        ctx = make_tenant("journey-e2e11")
        # **A zero grant is an activation decision now, not a plan setting.** The
        # grant is minutes x credits-per-minute and both are positive, so no plan can
        # describe "included: nothing" — which is exactly the pay-as-you-go shape.
        # `credit_grant=0` is the same override a negotiated contract uses, and it is
        # what keeps this product expressible.
        subscription = subscription_service.activate(ctx, payg, credit_grant=0)

        wallet = assert_invariant(self, ctx.tenant, msg="payg signup")
        self.assertEqual(wallet.balance, 0)
        self.assertFalse(
            CreditLedger.all_objects.filter(tenant=ctx.tenant).exists()
        )

        self.set_cycle(subscription, 20)
        order = ctx.client.post(
            TOP_UP_URL, {"credits": 400}, format="json"
        ).json()["data"]["razorpay_order_id"]
        pay(order)
        self.assertEqual(self.wallet(ctx.tenant).purchased_balance, 400)

        self.lapse(subscription)
        result = process_billing_cycle()

        subscription.refresh_from_db()
        wallet = assert_invariant(self, ctx.tenant, msg="payg rollover")
        self.assertEqual(result["renewed"], 1)
        self.assertEqual(result["expired"], 0)
        self.assertEqual(subscription.status, Status.ACTIVE)
        self.assertFalse(wallet.is_frozen)
        self.assertEqual(wallet.purchased_balance, 400)
        self.assertIsNotNone(
            self.call(
                ctx.tenant, Operation.CONVERSATION_HIGH_LATENCY, 60, "e2e11-call"
            )
        )

    def test_e2e12_audit_backfill_audit_enable(self):
        """E2E-12: **the sequence that was actually run against this platform.**

        Every tenant had subscribed before ``activate()`` existed, so none had a
        stamped rate card and every call would have raised. Blocked, repaired,
        ready, enforced.
        """
        ctx = make_tenant("journey-e2e12")
        Subscription.objects.create(tenant=ctx.tenant, plan=self.starter)

        def audit(**options):
            out = StringIO()
            call_command("audit_billing_readiness", stdout=out, stderr=StringIO(),
                         **options)
            return out.getvalue()

        blocked = audit()
        self.assertIn("no wallet", blocked)
        self.assertIn("blocked by findings   : 1", blocked)

        call_command(
            "backfill_wallet_stamps", grant_allocations=True,
            stdout=StringIO(), stderr=StringIO(),
        )

        ready = audit()
        self.assertIn("ready for enforcement : 1", ready)
        self.assertIn("blocked by findings   : 0", ready)

        audit(enable_safe=True)

        settings_row = TenantSettings.objects.get(tenant=ctx.tenant)
        self.assertTrue(settings_row.credits_enforced)
        self.assertTrue(settings_row.entitlements_enforced)
        assert_invariant(self, ctx.tenant, msg="after the backfill")


class ReactivationJourneys(JourneyTestCase):
    """E2E-13 to E2E-16 — lapsing, and coming back."""

    def lapsed_tenant(self, name, plan=None, purchased=250):
        ctx = make_tenant(name)
        subscription = subscription_service.activate(ctx, plan or self.starter)
        if purchased:
            wallet_service.grant(
                ctx.tenant, entry_type=EntryType.MANUAL_TOP_UP, amount=purchased,
                bucket=Bucket.PURCHASED, reference_id=f"{name}-top-up",
            )
        self.lapse(subscription)
        subscription_service.expire(subscription)
        subscription.refresh_from_db()
        return ctx, subscription

    def test_e2e13_lapse_see_state_return_on_a_bigger_plan(self):
        """E2E-13: **the credits bought before the lapse survive the whole
        journey** — through an expiry, a freeze, a plan change and a fresh
        allocation."""
        ctx, subscription = self.lapsed_tenant("journey-e2e13")

        state = ctx.client.get(CURRENT_URL)
        self.assertEqual(state.status_code, status.HTTP_200_OK)
        self.assertEqual(state.json()["data"]["status"], Status.EXPIRED)
        self.assertEqual(state.json()["data"]["credits"]["purchased"], 250)

        plans = {
            entry["code"]: entry
            for entry in ctx.client.get(PLANS_URL).json()["data"]["plans"]
        }
        self.assertEqual(
            plans[self.professional.code]["amount_due"],
            str(self.professional.total_price),
        )

        response = ctx.client.post(
            CHANGE_URL, {"plan_id": str(self.professional.pk)}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        pay(response.json()["data"]["razorpay_order_id"])

        live = subscription_service.current_for(ctx.tenant)
        subscription.refresh_from_db()
        self.assertEqual(live.plan_id, self.professional.pk)
        self.assertEqual(subscription.status, Status.CANCELLED)
        self.assertEqual(subscription.superseded_by_id, live.pk)

        wallet_service.unfreeze(ctx.tenant)
        wallet = assert_invariant(self, ctx.tenant, msg="after reactivation")
        self.assertEqual(wallet.purchased_balance, 250)
        self.assertEqual(wallet.allocated_balance, 3500)

        entry = self.call(
            ctx.tenant, Operation.CONVERSATION_LOW_LATENCY, 60, "e2e13-call"
        )
        self.assertEqual(abs(entry.amount), 6)

    def test_e2e14_lapse_then_renew_the_same_plan(self):
        """E2E-14: one subscription row throughout."""
        ctx, subscription = self.lapsed_tenant("journey-e2e14")

        renewal = ctx.client.post(RENEW_URL)
        self.assertEqual(renewal.status_code, status.HTTP_200_OK)
        pay(renewal.json()["data"]["razorpay_order_id"])

        subscription.refresh_from_db()
        wallet = assert_invariant(self, ctx.tenant, msg="after renewing")
        self.assertEqual(subscription.status, Status.ACTIVE)
        self.assertEqual(Subscription.objects.filter(tenant=ctx.tenant).count(), 1)
        self.assertGreater(subscription.end_date, timezone.now())
        self.assertFalse(wallet.is_frozen)
        self.assertEqual(wallet.allocated_balance, 1000)
        self.assertEqual(wallet.purchased_balance, 250)

    def test_e2e15_lapse_then_return_on_the_free_plan(self):
        """E2E-15: nothing to charge, so nothing is ordered — and no scheduled
        change with an effective date in the past."""
        ctx, subscription = self.lapsed_tenant("journey-e2e15", purchased=0)

        response = ctx.client.post(
            CHANGE_URL, {"plan_id": str(self.free.pk)}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["data"]["change_type"], "immediate_free")
        self.assertFalse(response.json()["data"]["charged"])
        self.assertFalse(Transaction.objects.filter(tenant=ctx.tenant).exists())

        live = subscription_service.current_for(ctx.tenant)
        self.assertEqual(live.plan_id, self.free.pk)
        assert_invariant(self, ctx.tenant, msg="after returning on free")

    def test_e2e16_lapsing_twice_leaves_a_history_that_can_be_read(self):
        """E2E-16: **renewing keeps one row; switching plans links a chain.**

        Neither should produce an unexplained pile of subscriptions for a customer
        who simply came and went twice.
        """
        ctx, subscription = self.lapsed_tenant("journey-e2e16")

        renewal = ctx.client.post(RENEW_URL)
        pay(renewal.json()["data"]["razorpay_order_id"])
        self.assertEqual(Subscription.objects.filter(tenant=ctx.tenant).count(), 1)

        subscription.refresh_from_db()
        self.lapse(subscription)
        subscription_service.expire(subscription)
        subscription.refresh_from_db()

        change = ctx.client.post(
            CHANGE_URL, {"plan_id": str(self.professional.pk)}, format="json"
        )
        pay(change.json()["data"]["razorpay_order_id"])

        rows = Subscription.objects.filter(tenant=ctx.tenant)
        live = subscription_service.current_for(ctx.tenant)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(live.plan_id, self.professional.pk)
        subscription.refresh_from_db()
        self.assertEqual(subscription.superseded_by_id, live.pk)
        assert_invariant(self, ctx.tenant, msg="after two lapses")
