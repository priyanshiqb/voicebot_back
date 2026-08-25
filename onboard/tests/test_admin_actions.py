"""Section 27.3 of the billing test plan — AD-23 to AD-25.

Plan and entitlement admin. Small, but each case guards a number that is frozen
somewhere else the moment it is used: a plan's allowances land in
``Subscription.plan_snapshot``, and an add-on's price lands on ``TenantEntitlement``.
Editing either after the fact would re-price something the tenant already holds.
"""

from decimal import Decimal

from django.contrib.admin.sites import AdminSite
from django.db import IntegrityError, transaction
from django.test import TestCase

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
