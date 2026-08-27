"""Cloning a plan.

The reason this exists is one specific mistake: a yearly plan carrying a monthly
plan's add-on prices. Block prices are charged **per cycle**, so ₹199 per 10 GB buys a
month on one plan and a whole year on the other — the same capacity for a twelfth of
the money, from a copy-paste nobody reviewed.

So the tests care about two things: that everything is copied, and that the things a
copy gets *wrong* are said out loud.
"""

from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from onboard.constants.entitlement_resource import EntitlementResource as Resource
from onboard.models import Plan
from onboard.tests.billing_factories import make_plan
from ops.services.plan import clone as clone_service
from ops.services.plan import config, readiness
from user.models import CustomUser, OperatorAuditLog

Role = CustomUser.PlatformRole
Action = OperatorAuditLog.Action
PLANS = "/api/v1/ops/plans/"


def operator(email, role=Role.MANAGER, permissions=None):
    return CustomUser.objects.create(
        email=email, username=email,
        user_type=CustomUser.UserType.ADMIN,
        platform_role=role,
        console_permissions=permissions or [],
    )


def codes(items):
    return {item["code"] for item in items}


class CloneServiceTests(TestCase):
    def setUp(self):
        self.source = make_plan(
            "clone-source", sort_order=2, allocated_credits=1000,
            base_price=Decimal("999.00"),
        )

    def test_it_copies_all_three_models(self):
        plan, _ = clone_service.clone(
            self.source, code="copy", name="Copy"
        )

        self.assertEqual(len(config.live_entitlements(plan)), 7)
        self.assertEqual(len(config.live_rates(plan)), 3)
        self.assertTrue(readiness.report(plan)["can_activate"])

    def test_the_copy_is_a_draft_however_live_the_original_was(self):
        self.source.status = Plan.Status.ACTIVE
        self.source.save(update_fields=["status", "updated_at"])

        plan, _ = clone_service.clone(self.source, code="c2", name="C2")

        self.assertEqual(plan.status, Plan.Status.DRAFT)
        self.assertFalse(plan.is_active)

    def test_identity_is_not_inherited(self):
        """`code` and `name` are unique columns, so a copy cannot have them."""
        plan, _ = clone_service.clone(self.source, code="c3", name="C3")

        self.assertEqual(plan.code, "c3")
        self.assertEqual(plan.name, "C3")
        self.assertNotEqual(plan.pk, self.source.pk)

    def test_the_settings_are_genuinely_copied(self):
        plan, _ = clone_service.clone(self.source, code="c4", name="C4")

        self.assertEqual(plan.base_price, self.source.base_price)
        self.assertEqual(plan.allocated_credits, self.source.allocated_credits)
        self.assertEqual(
            plan.allow_rollover_on_upgrade, self.source.allow_rollover_on_upgrade
        )
        source_agent = self.source.entitlements.get(resource=Resource.AGENT)
        copy_agent = plan.entitlements.get(resource=Resource.AGENT)
        self.assertEqual(copy_agent.included_quantity, source_agent.included_quantity)
        self.assertEqual(copy_agent.max_additional, source_agent.max_additional)

    def test_editing_the_copy_leaves_the_original_alone(self):
        plan, _ = clone_service.clone(self.source, code="c5", name="C5")

        plan.entitlements.filter(resource=Resource.AGENT).update(
            included_quantity=99
        )

        self.assertNotEqual(
            self.source.entitlements.get(resource=Resource.AGENT).included_quantity,
            99,
        )

    def test_the_copys_rates_start_now_not_when_the_original_did(self):
        """A copy is a new plan. Inheriting the original's `effective_from` would
        claim its rates were in force before the plan existed."""
        plan, _ = clone_service.clone(self.source, code="c6", name="C6")

        source_rate = config.live_rates(self.source)[0]
        copy_rate = config.live_rates(plan)[0]
        self.assertGreater(copy_rate.effective_from, source_rate.effective_from)


class CycleChangeReviewTests(TestCase):
    """Switching cycle is where the mispricing happens."""

    def setUp(self):
        self.monthly = make_plan("cycle-source", sort_order=2)
        self.monthly.entitlements.filter(resource=Resource.STORAGE_GB).update(
            is_addon_available=True, price_per_additional=Decimal("199.00"),
            included_quantity=10, addon_step=10, max_additional=90,
        )

    def test_prices_are_copied_verbatim_never_converted(self):
        """Twelve times is arithmetic; whether the annual price carries the same
        discount as the plan is a commercial decision. A wrong number somebody
        believes was calculated is harder to spot than a blank one."""
        plan, _ = clone_service.clone(
            self.monthly, code="yr", name="Yearly",
            billing_cycle=Plan.BillingCycleChoices.YEARLY,
        )

        self.assertEqual(plan.base_price, self.monthly.base_price)
        self.assertEqual(
            plan.entitlements.get(resource=Resource.STORAGE_GB).price_per_additional,
            Decimal("199.00"),
        )

    def test_a_cycle_change_is_called_out(self):
        _, review = clone_service.clone(
            self.monthly, code="yr2", name="Yearly 2",
            billing_cycle=Plan.BillingCycleChoices.YEARLY,
        )

        self.assertIn("CYCLE_CHANGED", codes(review))
        self.assertIn("copied as-is, not converted", str(review))

    def test_the_addons_that_need_repricing_are_named(self):
        _, review = clone_service.clone(
            self.monthly, code="yr3", name="Yearly 3",
            billing_cycle=Plan.BillingCycleChoices.YEARLY,
        )

        note = next(n for n in review if n["code"] == "ADDON_PRICES_NEED_REVIEW")
        self.assertIn(Resource.STORAGE_GB, note["resources"])

    def test_a_same_cycle_copy_says_nothing_about_prices(self):
        _, review = clone_service.clone(
            self.monthly, code="same", name="Same", sort_order=9
        )

        self.assertNotIn("CYCLE_CHANGED", codes(review))
        self.assertNotIn("ADDON_PRICES_NEED_REVIEW", codes(review))

    def test_an_inherited_sort_order_is_called_out(self):
        _, review = clone_service.clone(self.monthly, code="tied", name="Tied")

        self.assertIn("SORT_ORDER_COLLIDES", codes(review))

    def test_giving_the_copy_its_own_order_silences_that(self):
        _, review = clone_service.clone(
            self.monthly, code="ordered", name="Ordered", sort_order=7
        )

        self.assertNotIn("SORT_ORDER_COLLIDES", codes(review))

    def test_the_readiness_warning_still_catches_it_later(self):
        """Belt and braces. Clone says it at the moment of copying; readiness says it
        again at the moment of activating, when the source is no longer in mind."""
        plan, _ = clone_service.clone(
            self.monthly, code="yr4", name="Yearly 4",
            billing_cycle=Plan.BillingCycleChoices.YEARLY,
        )

        self.assertIn(
            "YEARLY_ADDONS_LOOK_MONTHLY", codes(readiness.warnings(plan))
        )


class CloneApiTests(TestCase):
    def setUp(self):
        self.source = make_plan("api-clone-source", sort_order=2)
        self.client = APIClient()
        self.client.force_authenticate(user=operator("cloner@ops.com"))
        self.url = f"{PLANS}{self.source.pk}/clone/"

    def test_it_returns_the_copy_and_what_to_review(self):
        response = self.client.post(self.url, {
            "reason": "yearly version for the enterprise segment",
            "code": "api-yearly", "name": "API Yearly",
            "billing_cycle": Plan.BillingCycleChoices.YEARLY,
        }, format="json")

        self.assertEqual(response.status_code, 201)
        data = response.data["data"]
        self.assertEqual(data["plan"]["status"], Plan.Status.DRAFT)
        self.assertEqual(data["cloned_from"]["id"], str(self.source.pk))
        self.assertIn("CYCLE_CHANGED", codes(data["review"]))

    def test_a_duplicate_code_is_refused(self):
        response = self.client.post(self.url, {
            "reason": "x", "code": self.source.code, "name": "Different",
        }, format="json")

        self.assertEqual(response.status_code, 409)

    def test_cloning_requires_the_write_grant(self):
        sub = APIClient()
        sub.force_authenticate(user=operator("subclone@ops.com", Role.SUB_MANAGER))

        response = sub.post(self.url, {
            "reason": "x", "code": "nope", "name": "Nope",
        }, format="json")

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Plan.objects.filter(code="nope").exists())

    def test_cloning_leaves_an_audit_row_naming_the_source(self):
        self.client.post(self.url, {
            "reason": "yearly version", "code": "audited", "name": "Audited",
        }, format="json")

        row = OperatorAuditLog.objects.get(action=Action.PLAN_CHANGE)
        self.assertEqual(row.payload["operation"], "clone")
        self.assertEqual(row.payload["source"], str(self.source.pk))

    def test_a_reason_is_required(self):
        response = self.client.post(self.url, {
            "code": "noreason", "name": "No Reason",
        }, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("REASON_REQUIRED", response.data["errors"])
