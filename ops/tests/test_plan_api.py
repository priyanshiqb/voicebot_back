"""`/api/v1/ops/plans/` — the endpoints commercial uses to define what is sold.

The services are tested separately; this is about the layer over them: who may reach
it, what a write requires, and whether an audit row lands.
"""

from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from onboard.constants.entitlement_resource import EntitlementResource as Resource
from onboard.models import Plan
from onboard.tests.billing_factories import make_plan, make_tenant
from ops.tests.test_plan_config_service import (
    entitlement_rows,
    plan_fields,
    rate_rows,
)
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


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def create_payload(**overrides):
    body = {
        "reason": "new tier for the enterprise segment",
        "plan": plan_fields(**overrides.pop("plan", {})),
        "entitlements": entitlement_rows(),
        "rates": rate_rows(),
    }
    body.update(overrides)
    # Decimals do not survive the JSON encoder the test client uses.
    body["plan"] = {
        k: (str(v) if isinstance(v, Decimal) else v) for k, v in body["plan"].items()
    }
    for row in body["entitlements"]:
        row["price_per_additional"] = str(row["price_per_additional"])
    return body


class AccessTests(TestCase):
    """Who may reach the console at all."""

    def test_an_anonymous_caller_is_refused(self):
        self.assertIn(APIClient().get(PLANS).status_code, (401, 403))

    def test_a_tenant_user_is_refused(self):
        """A tenant's own administrator is not platform staff. This is the boundary
        the whole app exists to make inspectable."""
        tenant_user = make_tenant("ops-outsider").user

        self.assertEqual(client_for(tenant_user).get(PLANS).status_code, 403)

    def test_a_superuser_without_a_console_role_is_refused(self):
        """`is_superuser` grants Django admin, not the console. An engineer should
        not silently gain operator access."""
        root = CustomUser.objects.create(
            email="root@x.com", username="root@x.com",
            is_superuser=True, is_staff=True,
        )

        self.assertEqual(client_for(root).get(PLANS).status_code, 403)

    def test_a_manager_may_read(self):
        self.assertEqual(
            client_for(operator("m@ops.com")).get(PLANS).status_code, 200
        )

    def test_a_sub_manager_may_read_without_any_grant(self):
        """Reading is not gated per-action — a console where support cannot look at
        a plan is not a console."""
        sub = operator("s@ops.com", Role.SUB_MANAGER)

        self.assertEqual(client_for(sub).get(PLANS).status_code, 200)


class WritePermissionTests(TestCase):
    """**The bug this pins.** `required_permission` is resolved from `self.action`
    before dispatch. Set inside a handler instead, the check would see `None` and let
    any platform staff write — the attribute arriving far too late to matter.
    """

    def setUp(self):
        self.plan = make_plan("perm")

    def test_a_sub_manager_without_the_grant_cannot_create(self):
        sub = operator("s1@ops.com", Role.SUB_MANAGER)

        response = client_for(sub).post(PLANS, create_payload(), format="json")

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Plan.objects.filter(code="ops-created").exists())

    def test_a_sub_manager_without_the_grant_cannot_activate(self):
        sub = operator("s2@ops.com", Role.SUB_MANAGER)

        response = client_for(sub).post(
            f"{PLANS}{self.plan.pk}/activate/", {"reason": "x"}, format="json"
        )

        self.assertEqual(response.status_code, 403)

    def test_a_sub_manager_without_the_grant_cannot_edit_rates(self):
        sub = operator("s3@ops.com", Role.SUB_MANAGER)

        response = client_for(sub).put(
            f"{PLANS}{self.plan.pk}/rates/",
            {"reason": "x", "rates": rate_rows()},
            format="json",
        )

        self.assertEqual(response.status_code, 403)

    def test_a_granted_sub_manager_may_write(self):
        sub = operator("s4@ops.com", Role.SUB_MANAGER, [Action.PLAN_CHANGE])

        response = client_for(sub).post(PLANS, create_payload(), format="json")

        self.assertEqual(response.status_code, 201)

    def test_every_write_route_is_gated(self):
        """Belt and braces: a route added later without naming its permission would
        be readable by staff, and this catches one that changes state."""
        sub = client_for(operator("s5@ops.com", Role.SUB_MANAGER))
        pk = self.plan.pk

        refused = [
            sub.post(PLANS, create_payload(), format="json"),
            sub.patch(f"{PLANS}{pk}/", {"reason": "x", "name": "N"}, format="json"),
            sub.put(f"{PLANS}{pk}/entitlements/",
                    {"reason": "x", "entitlements": entitlement_rows()}, format="json"),
            sub.put(f"{PLANS}{pk}/rates/",
                    {"reason": "x", "rates": rate_rows()}, format="json"),
            sub.post(f"{PLANS}{pk}/activate/", {"reason": "x"}, format="json"),
            sub.post(f"{PLANS}{pk}/retire/", {"reason": "x"}, format="json"),
        ]

        self.assertEqual([r.status_code for r in refused], [403] * 6)


class CreateTests(TestCase):
    def setUp(self):
        self.client = client_for(operator("creator@ops.com"))

    def test_it_creates_a_complete_draft(self):
        response = self.client.post(PLANS, create_payload(), format="json")

        self.assertEqual(response.status_code, 201)
        data = response.data["data"]
        self.assertEqual(data["plan"]["status"], Plan.Status.DRAFT)
        self.assertEqual(len(data["entitlements"]), 7)
        self.assertEqual(len(data["rates"]), 3)
        self.assertTrue(data["readiness"]["can_activate"])

    def test_a_missing_resource_is_refused_with_a_reason(self):
        """A resource with no allowance is not limited to zero — it is not limited
        at all, which is why the payload has to carry all seven."""
        body = create_payload()
        body["entitlements"] = [
            r for r in body["entitlements"] if r["resource"] != Resource.STORAGE_GB
        ]

        response = self.client.post(PLANS, body, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("storage gb", str(response.data["errors"]))

    def test_a_missing_tier_is_refused(self):
        body = create_payload()
        body["rates"] = body["rates"][:2]

        response = self.client.post(PLANS, body, format="json")

        self.assertEqual(response.status_code, 400)

    def test_an_absent_field_is_refused_even_though_the_model_defaults_it(self):
        """A default is a decision nobody made. `sort_order` defaults to 0, which is
        how two plans end up ordering arbitrarily against each other."""
        body = create_payload()
        del body["plan"]["sort_order"]

        response = self.client.post(PLANS, body, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("sort_order", response.data["errors"]["plan"])

    def test_a_present_null_is_an_answer(self):
        """"I chose unlimited" must be distinguishable from "I forgot this row"."""
        body = create_payload()
        for row in body["entitlements"]:
            if row["resource"] == Resource.CONTACT:
                row["included_quantity"] = None

        response = self.client.post(PLANS, body, format="json")

        self.assertEqual(response.status_code, 201)
        contact = next(
            e for e in response.data["data"]["entitlements"]
            if e["resource"] == Resource.CONTACT
        )
        self.assertIsNone(contact["included_quantity"])

    def test_a_paid_plan_that_renews_free_is_refused_at_activation(self):
        """**The check moved, and had to.**

        It used to be a database constraint on `base_price`. The price now lives on
        the basis rate row, so "is this plan free?" is a cross-table question and no
        column can answer it. The activation gate can, and it is the door every plan
        goes through before it reaches a customer.
        """
        from ops.services.plan import readiness

        body = create_payload(plan={"renews_without_payment": True})

        response = self.client.post(PLANS, body, format="json")
        self.assertEqual(response.status_code, 201)

        plan = Plan.objects.get(code=body["plan"]["code"])
        codes = [p["code"] for p in readiness.blocking_problems(plan)]
        self.assertIn("RENEWS_FREE_BUT_PRICED", codes)

    def test_a_write_without_a_reason_is_refused(self):
        body = create_payload()
        del body["reason"]

        response = self.client.post(PLANS, body, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("REASON_REQUIRED", response.data["errors"])

    def test_creating_leaves_an_audit_row(self):
        self.client.post(PLANS, create_payload(), format="json")

        row = OperatorAuditLog.objects.get(action=Action.PLAN_CHANGE)
        self.assertEqual(row.reason, "new tier for the enterprise segment")
        self.assertEqual(row.payload["operation"], "create")


class LifecycleApiTests(TestCase):
    def setUp(self):
        self.client = client_for(operator("life@ops.com"))
        self.plan = make_plan("api-life")
        self.plan.status = Plan.Status.DRAFT
        self.plan.save(update_fields=["status", "updated_at"])

    def url(self, suffix):
        return f"{PLANS}{self.plan.pk}/{suffix}/"

    def test_a_complete_plan_activates(self):
        response = self.client.post(
            self.url("activate"), {"reason": "launching"}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.status, Plan.Status.ACTIVE)

    def test_an_incomplete_plan_is_refused_and_told_why(self):
        self.plan.entitlements.get(resource=Resource.AGENT).soft_delete()

        response = self.client.post(
            self.url("activate"), {"reason": "launching"}, format="json"
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("PLAN_NOT_READY", response.data["errors"])
        codes = {p["code"] for p in response.data["data"]["blocking"]}
        self.assertIn("MISSING_ENTITLEMENTS", codes)

    def test_a_refused_activation_is_recorded_as_a_failure(self):
        """An operator repeatedly failing to activate is a signal, and it vanishes
        if only successes are kept."""
        self.plan.entitlements.get(resource=Resource.AGENT).soft_delete()

        self.client.post(self.url("activate"), {"reason": "trying"}, format="json")

        row = OperatorAuditLog.objects.get(action=Action.PLAN_CHANGE)
        self.assertEqual(row.outcome, OperatorAuditLog.Outcome.FAILED)
        self.assertIn("blocking", row.result)

    def test_retiring_says_who_stays_behind(self):
        from onboard.services import subscriptions as subscription_service

        self.plan.status = Plan.Status.ACTIVE
        self.plan.save(update_fields=["status", "updated_at"])
        subscription_service.activate(make_tenant("stays").tenant, self.plan)

        response = self.client.post(
            self.url("retire"), {"reason": "replaced by v2"}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("1 tenant(s) stay on it", response.data["message"])

    def test_readiness_is_readable_without_a_write_grant(self):
        sub = client_for(operator("reader@ops.com", Role.SUB_MANAGER))

        response = sub.get(self.url("readiness"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("can_activate", response.data["data"])


class RateSupersedeApiTests(TestCase):
    def setUp(self):
        self.client = client_for(operator("rates@ops.com"))

    def put_rates(self, plan, credits):
        """Send the card back unchanged except for one tier.

        `rate_rows()` uses its own defaults, which differ from the factory's on two
        tiers — sending it wholesale would supersede all three and say nothing about
        whether only the edited one moves.
        """
        from ops.services.plan import config

        rows = [
            {
                "operation": r.operation,
                "credits_per_unit": r.credits_per_unit,
                # Sent back too: a rate row carries both numbers now, and dropping
                # this one would strand the basis tier of a live plan -- which the
                # write guard refuses, correctly.
                "price_per_minute_inr": r.price_per_minute_inr,
                "unit": r.unit,
                "rounding": r.rounding,
                "minimum_units": r.minimum_units,
                "notes": r.notes or "",
            }
            for r in config.live_rates(plan)
        ]
        rows[0]["credits_per_unit"] = credits
        self.changed_operation = rows[0]["operation"]
        return self.client.put(
            f"{PLANS}{plan.pk}/rates/",
            {"reason": "repricing", "rates": rows},
            format="json",
        )

    def test_a_draft_edits_in_place(self):
        plan = make_plan("draft-rates")
        plan.status = Plan.Status.DRAFT
        plan.save(update_fields=["status", "updated_at"])
        before = plan.credit_rates.count()

        response = self.put_rates(plan, 11)

        self.assertEqual(response.status_code, 200)
        self.assertIn("updated", response.data["message"])
        self.assertEqual(plan.credit_rates.count(), before)

    def test_a_live_plan_supersedes(self):
        plan = make_plan("live-rates")
        before = plan.credit_rates.count()

        response = self.put_rates(plan, 11)

        self.assertEqual(response.status_code, 200)
        self.assertIn("superseded", response.data["message"])
        # Exactly one new row: only the edited tier supersedes. The other two were
        # sent back unchanged and must not gain a version boundary that says nothing.
        self.assertEqual(plan.credit_rates.count(), before + 1)
        self.assertEqual(
            plan.credit_rates.filter(effective_to__isnull=True).count(), 3
        )
        closed = plan.credit_rates.filter(effective_to__isnull=False)
        self.assertEqual(closed.count(), 1)
        self.assertEqual(closed.first().operation, self.changed_operation)


class ListTests(TestCase):
    def test_the_list_carries_completeness_and_subscribers(self):
        from onboard.services import subscriptions as subscription_service

        plan = make_plan("listed")
        subscription_service.activate(make_tenant("listed-t").tenant, plan)

        response = client_for(operator("list@ops.com")).get(PLANS)

        row = next(r for r in response.data["data"] if r["id"] == str(plan.pk))
        self.assertEqual(row["entitlements_configured"], 7)
        self.assertEqual(row["rates_configured"], 3)
        self.assertEqual(row["subscriber_count"], 1)
        self.assertTrue(row["is_complete"])

    def test_an_incomplete_plan_is_visible_as_such(self):
        plan = make_plan("half")
        plan.entitlements.get(resource=Resource.CRAWL_PAGE).soft_delete()

        response = client_for(operator("list2@ops.com")).get(PLANS)

        row = next(r for r in response.data["data"] if r["id"] == str(plan.pk))
        self.assertEqual(row["entitlements_configured"], 6)
        self.assertFalse(row["is_complete"])
