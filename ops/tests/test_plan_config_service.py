"""Creating, editing and moving a plan through its lifecycle.

The service layer, tested without HTTP. What matters here is what the database ends
up holding — the views are a thin layer over this, and testing them instead would
test the routing rather than the rules.
"""

from decimal import Decimal

from django.test import TestCase

from onboard.constants.entitlement_resource import EntitlementResource as Resource
from onboard.constants.reset_period import ResetPeriod
from onboard.models import Plan, Subscription
from onboard.services import subscriptions as subscription_service
from onboard.tests.billing_factories import make_plan, make_tenant
from ops.services.plan import config, lifecycle, readiness
from wallet.models import CreditRate

Operation = CreditRate.Operation


def plan_fields(**overrides):
    fields = {
        "code": "ops-created",
        "name": "Ops Created",
        "description": "",
        "sort_order": 5,
        "is_most_popular": False,
        "plan_type": Plan.PlanTypeChoices.PAID,
        "discount_percentage": Decimal("0.00"),
        "tax_percentage": Decimal("18.00"),
        "currency": Plan.CurrencyChoices.INR,
        "billing_cycle": Plan.BillingCycleChoices.MONTHLY,
        # Sold by the minute: a band, a basis, and a rupee price on the
        # basis row. Without these the plan cannot be activated.
        "min_minutes": None,
        "max_minutes": 1000,
        "credit_basis": Operation.CONVERSATION_HIGH_LATENCY,
        "cost_per_additional_credit": Decimal("1.20"),
        "low_balance_threshold": 100,
        "allow_rollover_on_renewal": False,
        "allow_rollover_on_upgrade": True,
        "allow_rollover_on_downgrade": False,
        "max_rollover_credits": None,
        "allow_use_after_expiry": False,
        "renews_without_payment": False,
    }
    fields.update(overrides)
    return fields


def entitlement_rows(**overrides):
    rows = []
    for resource in Resource.values:
        row = {
            "resource": resource,
            "included_quantity": 10,
            "reset_period": (
                ResetPeriod.MONTHLY
                if resource == Resource.CAMPAIGN_MONTHLY
                else ResetPeriod.NONE
            ),
            "is_addon_available": False,
            "price_per_additional": Decimal("0.00"),
            "addon_step": 1,
            "max_additional": None,
        }
        row.update(overrides.get(resource, {}))
        rows.append(row)
    return rows


def rate_rows(**overrides):
    rows = []
    for operation in Operation.values:
        row = {
            "operation": operation,
            "credits_per_unit": 5,
            "price_per_minute_inr": Decimal("2.2500"),
            "unit": CreditRate.Unit.PER_MINUTE,
            "rounding": CreditRate.Rounding.CEIL,
            "minimum_units": 1,
            "notes": "",
        }
        row.update(overrides.get(operation, {}))
        rows.append(row)
    return rows


class CreateTests(TestCase):
    def test_it_creates_the_plan_and_all_eleven_rows(self):
        plan = config.create(
            plan_fields=plan_fields(),
            entitlements=entitlement_rows(),
            rates=rate_rows(),
        )

        self.assertEqual(len(config.live_entitlements(plan)), 7)
        self.assertEqual(len(config.live_rates(plan)), 3)

    def test_a_created_plan_is_a_draft_and_not_sellable(self):
        """Complete is not the same as correct. Somebody still has to look at it."""
        plan = config.create(
            plan_fields=plan_fields(),
            entitlements=entitlement_rows(),
            rates=rate_rows(),
        )

        self.assertEqual(plan.status, Plan.Status.DRAFT)
        self.assertFalse(plan.is_active)
        self.assertFalse(
            Plan.objects.filter(pk=plan.pk, is_active=True).exists()
        )

    def test_a_created_plan_is_immediately_complete(self):
        plan = config.create(
            plan_fields=plan_fields(),
            entitlements=entitlement_rows(),
            rates=rate_rows(),
        )

        self.assertTrue(readiness.report(plan)["can_activate"])

    def test_a_failed_create_leaves_nothing_behind(self):
        """Atomic on purpose. A plan row written before a failing entitlement is
        exactly the half-configured thing this module exists to prevent."""
        rows = entitlement_rows()
        rows[3]["addon_step"] = 0  # ck_entitlement_addon_step_positive

        with self.assertRaises(Exception):
            config.create(
                plan_fields=plan_fields(code="doomed", name="Doomed"),
                entitlements=rows,
                rates=rate_rows(),
            )

        self.assertFalse(Plan.objects.filter(code="doomed").exists())


class EntitlementGridTests(TestCase):
    def setUp(self):
        self.plan = make_plan("grid")

    def test_it_updates_rows_in_place(self):
        before = {r.resource: r.pk for r in config.live_entitlements(self.plan)}
        rows = entitlement_rows()
        rows[0]["included_quantity"] = 99

        config.replace_entitlements(self.plan, rows)

        after = {r.resource: r.pk for r in config.live_entitlements(self.plan)}
        self.assertEqual(before, after, "primary keys should not churn")

    def test_it_creates_a_row_that_was_missing(self):
        self.plan.entitlements.get(resource=Resource.STORAGE_GB).soft_delete()
        self.assertFalse(readiness.report(self.plan)["can_activate"])

        config.replace_entitlements(self.plan, entitlement_rows())

        self.assertTrue(readiness.report(self.plan)["can_activate"])

    def test_the_new_values_are_what_is_stored(self):
        rows = entitlement_rows(**{
            Resource.AGENT: {
                "included_quantity": 4,
                "is_addon_available": True,
                "price_per_additional": Decimal("499.00"),
                "addon_step": 1,
                "max_additional": 6,
            }
        })

        config.replace_entitlements(self.plan, rows)

        agent = self.plan.entitlements.get(resource=Resource.AGENT)
        self.assertEqual(agent.included_quantity, 4)
        self.assertTrue(agent.is_addon_available)
        self.assertEqual(agent.max_additional, 6)


class RateCardTests(TestCase):
    def setUp(self):
        self.plan = make_plan("rates")

    def test_a_draft_edits_its_rates_in_place(self):
        """Nothing has been stamped onto a wallet yet, so there is no history to
        preserve — and a version boundary would mean nothing."""
        before = {r.operation: r.pk for r in config.live_rates(self.plan)}
        rows = rate_rows(**{Operation.STATIC_REEL: {"credits_per_unit": 9}})

        config.replace_rates(self.plan, rows, supersede=False)

        after = {r.operation: r.pk for r in config.live_rates(self.plan)}
        self.assertEqual(before, after)
        self.assertEqual(
            self.plan.credit_rates.get(
                operation=Operation.STATIC_REEL, effective_to__isnull=True
            ).credits_per_unit,
            9,
        )

    def test_a_live_plan_supersedes_rather_than_updates(self):
        """Every historical charge has to stay reproducible, so the old row is
        closed rather than overwritten."""
        original = self.plan.credit_rates.get(
            operation=Operation.STATIC_REEL, effective_to__isnull=True
        )
        rows = rate_rows(**{Operation.STATIC_REEL: {"credits_per_unit": 9}})

        config.replace_rates(self.plan, rows, supersede=True)

        original.refresh_from_db()
        self.assertIsNotNone(original.effective_to, "the old row must be closed")
        current = self.plan.credit_rates.get(
            operation=Operation.STATIC_REEL, effective_to__isnull=True
        )
        self.assertNotEqual(current.pk, original.pk)
        self.assertEqual(current.credits_per_unit, 9)

    def test_only_one_open_row_survives_per_tier(self):
        """`uniq_current_rate_per_plan_operation` permits exactly one, which is why
        the close has to precede the insert."""
        config.replace_rates(
            self.plan,
            rate_rows(**{Operation.STATIC_REEL: {"credits_per_unit": 9}}),
            supersede=True,
        )

        self.assertEqual(len(config.live_rates(self.plan)), 3)

    def test_an_unchanged_rate_is_not_superseded(self):
        """Otherwise every save fills the history with rows that say nothing."""
        before = {r.operation: r.pk for r in config.live_rates(self.plan)}

        # Both numbers, because a rate row now carries both. Copying only the credit
        # price would leave the rupee price differing from the fixture default, which
        # is a real change and would correctly supersede.
        config.replace_rates(self.plan, rate_rows(**{
            r.operation: {
                "credits_per_unit": r.credits_per_unit,
                "price_per_minute_inr": r.price_per_minute_inr,
            }
            for r in config.live_rates(self.plan)
        }), supersede=True)

        after = {r.operation: r.pk for r in config.live_rates(self.plan)}
        self.assertEqual(before, after)


class LifecycleTests(TestCase):
    def setUp(self):
        self.plan = make_plan("lifecycle")
        self.plan.status = Plan.Status.DRAFT
        self.plan.save(update_fields=["status", "updated_at"])

    def test_a_complete_draft_activates(self):
        lifecycle.activate(self.plan)

        self.plan.refresh_from_db()
        self.assertEqual(self.plan.status, Plan.Status.ACTIVE)
        self.assertTrue(self.plan.is_active)

    def test_an_incomplete_draft_is_refused(self):
        """The gate the module is built around."""
        self.plan.entitlements.get(resource=Resource.CONTACT).soft_delete()

        with self.assertRaises(lifecycle.PlanNotReady) as caught:
            lifecycle.activate(self.plan)

        self.assertIn(
            "MISSING_ENTITLEMENTS", {p["code"] for p in caught.exception.problems}
        )
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.status, Plan.Status.DRAFT)
        self.assertFalse(self.plan.is_active)

    def test_a_refused_activation_leaves_the_plan_untouched(self):
        rate = config.live_rates(self.plan)[0]
        rate.soft_delete()

        with self.assertRaises(lifecycle.PlanNotReady):
            lifecycle.activate(self.plan)

        self.plan.refresh_from_db()
        self.assertFalse(self.plan.is_active)

    def test_retiring_stops_new_subscriptions(self):
        lifecycle.activate(self.plan)

        lifecycle.retire(self.plan)

        self.plan.refresh_from_db()
        self.assertEqual(self.plan.status, Plan.Status.RETIRED)
        self.assertFalse(
            Plan.objects.filter(pk=self.plan.pk, is_active=True).exists()
        )

    def test_retiring_reports_who_stays_behind(self):
        """Existing subscribers keep the plan — their terms are frozen on their own
        subscription, not read from the catalogue row."""
        lifecycle.activate(self.plan)
        for name in ("stay-1", "stay-2"):
            subscription_service.activate(make_tenant(name).tenant, self.plan)

        result = lifecycle.retire(self.plan)

        self.assertEqual(result["subscribers_remaining"], 2)
        self.assertEqual(
            Subscription.objects.filter(
                plan=self.plan, status=Subscription.SubscriptionStatusChoices.ACTIVE
            ).count(),
            2,
        )

    def test_a_retired_plan_comes_back_through_the_same_gate(self):
        lifecycle.activate(self.plan)
        lifecycle.retire(self.plan)
        self.plan.entitlements.get(resource=Resource.AGENT).soft_delete()

        with self.assertRaises(lifecycle.PlanNotReady):
            lifecycle.activate(self.plan)

    def test_a_draft_can_be_retired_without_ever_being_active(self):
        """There is no delete verb. Retire is the one button."""
        lifecycle.retire(self.plan)

        self.plan.refresh_from_db()
        self.assertEqual(self.plan.status, Plan.Status.RETIRED)


class ImpactCountTests(TestCase):
    def test_it_counts_tenants_on_the_plan_now(self):
        plan = make_plan("counted")
        for name in ("a", "b", "c"):
            subscription_service.activate(make_tenant(f"count-{name}").tenant, plan)

        self.assertEqual(config.subscriber_count(plan), 3)

    def test_cancelled_and_expired_subscriptions_are_history(self):
        plan = make_plan("historic")
        ctx = make_tenant("historic-tenant")
        subscription = subscription_service.activate(ctx.tenant, plan)
        Subscription.objects.filter(pk=subscription.pk).update(
            status=Subscription.SubscriptionStatusChoices.EXPIRED
        )

        self.assertEqual(config.subscriber_count(plan), 0)

    def test_the_list_annotation_agrees_with_the_single_count(self):
        """The list annotates rather than looping — a naive version would be two
        queries per row, and `distinct=True` is what stops the joins multiplying."""
        plan = make_plan("annotated")
        subscription_service.activate(make_tenant("annotated-t").tenant, plan)

        row = config.annotated_queryset().get(pk=plan.pk)

        self.assertEqual(row.subscriber_count, config.subscriber_count(plan))
        self.assertEqual(row.entitlements_configured, 7)
        self.assertEqual(row.rates_configured, 3)
