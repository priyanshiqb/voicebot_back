"""Section 27.3 of the billing test plan — AD-23 to AD-25, and AD-27 to AD-29.

Plan and entitlement admin. Small, but each case guards a number that is frozen
somewhere else the moment it is used: a plan's allowances land in
``Subscription.plan_snapshot``, and an add-on's price lands on ``TenantEntitlement``.
Editing either after the fact would re-price something the tenant already holds.
"""

from decimal import Decimal

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase

from onboard.admin.plan import PlanAdmin
from onboard.admin.plan_entitlement import PlanEntitlementInline
from onboard.admin.tenant_entitlement import TenantEntitlementAdmin
from onboard.constants.entitlement_resource import EntitlementResource
from onboard.models import Plan, PlanEntitlement, TenantEntitlement
from onboard.tests.billing_factories import make_plan

Resource = EntitlementResource


class PlanEntitlementAdminTests(TestCase):
    """AD-23 to AD-24."""

    def setUp(self):
        self.site = AdminSite()
        self.plan = make_plan("admin-plan", with_entitlements=False)

    def test_ad23_allowances_are_edited_on_the_plan_that_prices_them(self):
        """AD-23: **"10 agents, +1 at ₹299" is a sentence about a plan**, not a
        standalone record. A separate page would have you cross-referencing seven
        rows against the plan you are actually pricing."""
        self.assertIn(PlanEntitlementInline, PlanAdmin.inlines)

        inline = PlanEntitlementInline(Plan, self.site)
        for field in (
            "resource", "included_quantity", "is_addon_available",
            "price_per_additional", "addon_step", "max_additional",
        ):
            self.assertIn(field, inline.fields)

        # And it really is editable there, not a read-only display.
        self.assertFalse(getattr(inline, "readonly_fields", ()))

    def test_ad24_an_unlimited_allowance_cannot_also_be_sold(self):
        """AD-24: **incoherent, and worse than merely wrong.**

        The effective limit is ``included + purchased``. With ``included`` meaning
        unlimited there is nothing for a purchase to add to, so the calculation
        stops having an answer rather than producing a bad one.
        """
        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                PlanEntitlement.objects.create(
                    plan=self.plan,
                    resource=Resource.AGENT,
                    included_quantity=None,
                    is_addon_available=True,
                    price_per_additional=Decimal("299.00"),
                )

        self.assertIn("ck_entitlement_no_addon_when_unlimited", str(caught.exception))

        # The same row without the add-on is fine.
        PlanEntitlement.objects.create(
            plan=self.plan, resource=Resource.AGENT,
            included_quantity=None, is_addon_available=False,
        )


class PlanAdminFormTests(TestCase):
    """AD-27 to AD-29."""

    def setUp(self):
        self.admin = PlanAdmin(Plan, AdminSite())
        self.request = RequestFactory().get("/admin/")
        self.request.user = get_user_model().objects.create_superuser(
            email="plan-admin@example.com", username="plan-admin", password="x"
        )

    def test_ad27_the_plan_page_opens_and_can_express_a_minutes_plan(self):
        """AD-27: **a fieldset naming a dropped column passes ``manage.py check``.**

        It only fails when the page builds its form, so dropping
        ``Plan.allocated_credits`` left the add and change pages raising
        ``FieldError`` while every check stayed green. Build both forms.
        """
        add_form = self.admin.get_form(self.request, None)
        plan = make_plan("admin-form-plan", with_entitlements=False)
        self.admin.get_form(self.request, plan)

        for field in (
            "min_minutes", "max_minutes", "credit_basis",
            "low_balance_percent", "max_rollover_ratio",
        ):
            self.assertIn(field, add_form.base_fields)
        self.assertNotIn("allocated_credits", add_form.base_fields)

    def test_ad28_status_moves_only_through_the_console_gate(self):
        """AD-28: **the activation gate has no side door here.**

        ``status`` is read-only, and the bulk activate/deactivate actions are gone:
        they write ``is_active`` with ``.update()``, which skips the readiness check
        entirely -- and a deactivate is undone by the plan's next save anyway.
        """
        form = self.admin.get_form(self.request, None)
        self.assertNotIn("status", form.base_fields)
        self.assertNotIn("is_active", form.base_fields)

        actions = self.admin.get_actions(self.request)
        self.assertNotIn("activate_selected", actions)
        self.assertNotIn("deactivate_selected", actions)

    def test_ad29_an_inverted_band_is_a_form_error_not_a_500(self):
        """AD-29: the admin form validates model constraints before saving, so the
        band constraint reaches the page as a message rather than an IntegrityError."""
        with self.assertRaises(ValidationError) as caught:
            Plan(min_minutes=500, max_minutes=100).validate_constraints()

        self.assertIn("ck_plan_minutes_band_valid", str(caught.exception))


class TenantEntitlementAdminTests(TestCase):
    """AD-25."""

    def test_ad25_frozen_prices_are_not_editable(self):
        """AD-25: **captured when the extras were last bought.**

        Changing them here would re-price capacity the tenant already holds, and
        the next renewal would invoice a number they never agreed to.
        """
        admin = TenantEntitlementAdmin(TenantEntitlement, AdminSite())
        readonly = set(admin.readonly_fields)

        self.assertIn("price_per_unit", readonly)
        self.assertIn("addon_step", readonly)
        self.assertIn("recurring_charge_display", readonly)
