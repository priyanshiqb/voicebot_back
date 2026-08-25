"""Section 11 of the billing test plan — EN-01 to EN-36.

Entitlements are **gauges**: held, then released. Nothing here ever touches the credit
wallet, and EN-02 is the test that says why — deleting an agent gives the allowance
back, which is a question a flow modelled as credits could never answer without owing
a refund.

Usage is counted live from the rows that *are* the resource. There is no counter to
drift and no reset job to forget, so these tests create real agents, contacts and
files rather than seeding a number.
"""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from onboard.constants.entitlement_resource import EntitlementResource
from onboard.models import PlanEntitlement, Subscription, TenantEntitlement
from onboard.services import entitlements as entitlement_service
from onboard.services import subscriptions as subscription_service
from onboard.tests.billing_factories import (
    add_entitlements,
    make_agent,
    make_campaign,
    make_contact,
    make_kb_file,
    make_kb_website,
    make_knowledge_base,
    make_plan,
    make_tenant,
)
from tenant.models import TenantSettings, TenantUserMapping
from wallet.models import CreditLedger

Resource = EntitlementResource
GIGABYTE = 1024 ** 3


class EntitlementTestCase(TestCase):
    """A tenant on a plan with small, exhaustible allowances."""

    def setUp(self):
        self.ctx = make_tenant("entitlements")
        self.tenant = self.ctx.tenant
        self.plan = make_plan("entitlements-plan")
        self.subscription = subscription_service.activate(self.tenant, self.plan)

    def enforce(self, enabled=True):
        settings_row = TenantSettings.for_tenant(self.tenant.id)
        settings_row.entitlements_enforced = enabled
        settings_row.save(update_fields=["entitlements_enforced"])
        return settings_row

    def usage(self, resource):
        return entitlement_service.current_usage(self.tenant.id, resource)


class UsageCountingTests(EntitlementTestCase):
    """EN-01 to EN-10 — counting what the tenant is holding right now."""

    def test_en01_agents_are_counted_from_live_rows(self):
        """EN-01."""
        make_agent(self.tenant)
        make_agent(self.tenant)

        self.assertEqual(self.usage(Resource.AGENT), 2)

    def test_en02_deleting_an_agent_frees_the_allowance(self):
        """EN-02: **the delete-test, and the whole reason these are not credits.**

        A credit spent is gone; a seat given back is available again. Modelling this
        as a flow would mean owing a refund every time a tenant tidied up.
        """
        first = make_agent(self.tenant)
        make_agent(self.tenant)
        self.assertEqual(self.usage(Resource.AGENT), 2)

        first.delete()

        self.assertEqual(self.usage(Resource.AGENT), 1)

    def test_en03_users_are_counted_from_the_tenant_mapping(self):
        """EN-03: ``make_tenant`` already creates one, so this is 1 + 1."""
        extra = make_tenant("entitlements-extra-user")
        TenantUserMapping.objects.filter(user=extra.user).update(tenant=self.tenant)

        self.assertEqual(self.usage(Resource.USER), 2)

    def test_en04_contacts_are_counted(self):
        """EN-04."""
        make_contact(self.tenant)
        make_contact(self.tenant)
        make_contact(self.tenant)

        self.assertEqual(self.usage(Resource.CONTACT), 3)

    def test_en05_knowledge_bases_are_counted(self):
        """EN-05."""
        make_knowledge_base(self.tenant)

        self.assertEqual(self.usage(Resource.KNOWLEDGE_BASE), 1)

    def test_en06_crawled_pages_are_counted_one_per_website_row(self):
        """EN-06."""
        make_kb_website(self.tenant)
        make_kb_website(self.tenant)

        self.assertEqual(self.usage(Resource.CRAWL_PAGE), 2)

    def test_en07_campaigns_are_counted_within_the_subscriptions_own_window(self):
        """EN-07: **the one windowed resource.**

        What is limited is the act of setting up a campaign, not the row that
        results — so a previous window's campaigns do not count against this one.

        The window is anchored to the subscription rather than the calendar. Anchored
        to the 1st, a cycle running 5 Aug to 4 Sep would contain two allowances, and
        a yearly cycle twelve.
        """
        from core.models.campaign import Campaign

        Subscription.objects.filter(pk=self.subscription.pk).update(
            created_at=timezone.now() - timedelta(days=20)
        )
        self.subscription.refresh_from_db()

        current = make_campaign(self.tenant)
        stale = make_campaign(self.tenant)
        Campaign.objects.filter(pk=stale.pk).update(
            created_at=timezone.now() - timedelta(days=45)
        )

        self.assertEqual(
            entitlement_service.current_usage(
                self.tenant.id, Resource.CAMPAIGN_MONTHLY,
                subscription=self.subscription,
            ),
            1,
        )
        self.assertTrue(Campaign.objects.filter(pk=current.pk).exists())

    def test_en08_storage_rounds_up_to_the_next_gigabyte(self):
        """EN-08: **10.2 GB reports 11.**

        A tenant holding 10.2 GB on a 10 GB plan is over, and reporting 10 would
        make the limit look satisfied while the disk says otherwise.
        """
        make_kb_file(self.tenant, 10 * GIGABYTE)
        make_kb_file(self.tenant, GIGABYTE // 5)

        self.assertEqual(self.usage(Resource.STORAGE_GB), 11)

    def test_en09_another_tenants_rows_are_never_counted(self):
        """EN-09: a leak here refuses one tenant's work because of another's."""
        other = make_tenant("entitlements-other")
        for _ in range(5):
            make_agent(other.tenant)
        make_agent(self.tenant)

        self.assertEqual(self.usage(Resource.AGENT), 1)

    def test_en10_an_unknown_resource_counts_as_nothing_and_says_so(self):
        """EN-10: a quota lookup must never be why an unrelated feature breaks."""
        with self.assertLogs("onboard.services.entitlements", level="WARNING"):
            self.assertEqual(self.usage("teleportation"), 0)


class LimitTests(EntitlementTestCase):
    """EN-11 to EN-14 — included plus purchased."""

    def test_en11_the_limit_is_included_plus_purchased(self):
        """EN-11."""
        TenantEntitlement.objects.create(
            tenant=self.tenant, resource=Resource.AGENT,
            additional_quantity=3, price_per_unit=Decimal("500.00"), addon_step=1,
        )

        self.assertEqual(
            entitlement_service.limit_for(
                self.tenant.id, self.subscription, Resource.AGENT
            ),
            5,
        )

    def test_en12_no_included_quantity_means_unlimited(self):
        """EN-12: ``None`` propagates all the way to ``check``, which then never
        refuses. 'Unlimited' has to be a distinct value from a very large number,
        or somebody eventually hits it."""
        unlimited_plan = make_plan("entitlements-unlimited", with_entitlements=False)
        PlanEntitlement.objects.create(
            plan=unlimited_plan, resource=Resource.AGENT,
            included_quantity=None, is_addon_available=False,
        )
        subscription = subscription_service.activate(
            make_tenant("entitlements-unlimited-tenant"), unlimited_plan
        )

        self.assertIsNone(
            entitlement_service.limit_for(
                subscription.tenant_id, subscription, Resource.AGENT
            )
        )

    def test_en13_without_purchased_extras_the_limit_is_what_the_plan_included(self):
        """EN-13."""
        self.assertEqual(
            entitlement_service.limit_for(
                self.tenant.id, self.subscription, Resource.AGENT
            ),
            2,
        )

    def test_en14_the_included_allowance_comes_from_the_snapshot(self):
        """EN-14: **raising a plan's cap must not silently change what an existing
        subscriber agreed to** — in either direction. Lowering it is the dangerous
        one: it would put working accounts over their limit overnight."""
        self.plan.entitlements.filter(resource=Resource.AGENT).update(
            included_quantity=50
        )

        self.assertEqual(
            entitlement_service.limit_for(
                self.tenant.id, self.subscription, Resource.AGENT
            ),
            2,
        )


class ApplyAddonTests(EntitlementTestCase):
    """EN-15 to EN-20 — recording purchased capacity."""

    def apply(self, units, resource=Resource.AGENT):
        return entitlement_service.apply_addon(
            tenant_id=self.tenant.id,
            subscription=self.subscription,
            resource=resource,
            additional_units=units,
        )

    def test_en15_the_first_purchase_freezes_the_price_it_recurs_at(self):
        """EN-15: frozen so a later price rise does not re-price capacity the
        tenant already holds."""
        row = self.apply(3)

        self.assertEqual(row.additional_quantity, 3)
        self.assertEqual(row.price_per_unit, Decimal("500.00"))
        self.assertEqual(row.addon_step, 1)

    def test_en16_buying_more_re_prices_the_whole_block(self):
        """EN-16: **"you have 5 extra agents at ₹600 each", not five rows at four
        historical prices.** The tenant is quoted that way and invoiced that way."""
        self.apply(3)
        self.plan.entitlements.filter(resource=Resource.AGENT).update(
            price_per_additional=Decimal("600.00")
        )

        row = self.apply(2)

        self.assertEqual(row.additional_quantity, 5)
        self.assertEqual(row.price_per_unit, Decimal("600.00"))

    def test_en17_the_recurring_charge_is_per_whole_block(self):
        """EN-17: matching ``PlanEntitlement.price_for`` exactly — the quote a
        tenant is shown and the amount they are charged have to come out of the
        same arithmetic."""
        self.plan.entitlements.filter(resource=Resource.STORAGE_GB).update(
            price_per_additional=Decimal("199.00"), addon_step=10
        )

        row = self.apply(12, Resource.STORAGE_GB)

        self.assertEqual(row.recurring_charge, Decimal("398.00"))

    def test_en18_the_purchase_records_when_and_under_which_cycle(self):
        """EN-18."""
        row = self.apply(1)

        self.assertIsNotNone(row.purchased_at)
        self.assertEqual(row.subscription_id, self.subscription.pk)

    def test_en19_a_resource_the_plan_does_not_offer_is_refused(self):
        """EN-19: returns ``None`` and logs rather than raising — this runs from a
        webhook, where raising would make Razorpay retry a payment we accepted."""
        bare_plan = make_plan("entitlements-bare", with_entitlements=False)
        add_entitlements(bare_plan, {Resource.AGENT: (2, Decimal("500.00"), 1, 10)})
        subscription = subscription_service.activate(
            make_tenant("entitlements-bare-tenant"), bare_plan
        )

        with self.assertLogs("onboard.services.entitlements", level="ERROR"):
            result = entitlement_service.apply_addon(
                tenant_id=subscription.tenant_id, subscription=subscription,
                resource=Resource.STORAGE_GB, additional_units=10,
            )

        self.assertIsNone(result)

    def test_en20_buying_capacity_never_writes_to_the_wallet(self):
        """EN-20: **the boundary, stated as a test.**

        A ledger row here would mean a seat had been paid for in credits, and then
        deleting the agent would raise a refund question that a gauge does not have.
        """
        before = CreditLedger.all_objects.filter(tenant=self.tenant).count()

        self.apply(3)

        self.assertEqual(
            CreditLedger.all_objects.filter(tenant=self.tenant).count(), before
        )


class CheckTests(EntitlementTestCase):
    """EN-21 to EN-30 — refusing to exceed a limit, once enforcement is on."""

    def check(self, resource=Resource.AGENT, **kwargs):
        return entitlement_service.check(self.tenant, resource, **kwargs)

    def test_en21_under_the_limit_nothing_happens(self):
        """EN-21."""
        self.enforce()
        make_agent(self.tenant)

        self.check()

    def test_en22_the_request_is_counted_before_it_is_allowed(self):
        """EN-22: at 2 of 2, asking for one more is over — the check is on
        ``used + requested``, not on ``used`` alone."""
        self.enforce()
        make_agent(self.tenant)
        make_agent(self.tenant)

        with self.assertRaises(entitlement_service.QuotaExceeded):
            self.check()

    def test_en23_going_over_while_enforcement_is_off_only_logs(self):
        """EN-23: **switching caps on for accounts already over them would break
        working setups with no warning.** The first thing anyone learns is a log
        line, not a broken workflow."""
        for _ in range(5):
            make_agent(self.tenant)

        with self.assertLogs("onboard.services.entitlements", level="WARNING") as logs:
            self.check()

        self.assertIn("would refuse", "".join(logs.output))

    def test_en24_the_refusal_carries_the_numbers_behind_it(self):
        """EN-24: "3 of 2 agents in use" tells someone what to do next; "forbidden"
        does not."""
        self.enforce()
        for _ in range(3):
            make_agent(self.tenant)

        with self.assertRaises(entitlement_service.QuotaExceeded) as caught:
            self.check(requested=1)

        self.assertEqual(caught.exception.resource, Resource.AGENT)
        self.assertEqual(caught.exception.used, 3)
        self.assertEqual(caught.exception.limit, 2)
        self.assertEqual(caught.exception.requested, 1)

    def test_en25_a_bulk_request_is_refused_on_the_whole_amount(self):
        """EN-25: importing 200 contacts into a 100-contact plan must fail as one
        request, not sail through because each row is individually under."""
        self.enforce()
        self.assertEqual(
            entitlement_service.limit_for(
                self.tenant.id, self.subscription, Resource.CONTACT
            ),
            100,
        )

        entitlement_service.check(self.tenant, Resource.CONTACT, requested=100)

        with self.assertRaises(entitlement_service.QuotaExceeded) as caught:
            entitlement_service.check(self.tenant, Resource.CONTACT, requested=101)

        self.assertEqual(caught.exception.requested, 101)

    def test_en26_an_unlimited_resource_never_refuses(self):
        """EN-26."""
        unlimited_plan = make_plan(
            "entitlements-check-unlimited", with_entitlements=False
        )
        PlanEntitlement.objects.create(
            plan=unlimited_plan, resource=Resource.AGENT,
            included_quantity=None, is_addon_available=False,
        )
        other = make_tenant("entitlements-check-unlimited-tenant")
        subscription_service.activate(other, unlimited_plan)
        settings_row = TenantSettings.for_tenant(other.tenant.id)
        settings_row.entitlements_enforced = True
        settings_row.save(update_fields=["entitlements_enforced"])

        entitlement_service.check(other.tenant, Resource.AGENT, requested=999999)

    def test_en27_a_tenant_with_no_live_plan_is_left_alone(self):
        """EN-27: **no live plan means no agreed limits.** Whatever gates access in
        that state is the subscription's business, not the quota checker's — and
        refusing here would double-punish an expired tenant."""
        self.enforce()
        stranger = make_tenant("entitlements-no-plan")
        settings_row = TenantSettings.for_tenant(stranger.tenant.id)
        settings_row.entitlements_enforced = True
        settings_row.save(update_fields=["entitlements_enforced"])
        for _ in range(50):
            make_agent(stranger.tenant)

        entitlement_service.check(stranger.tenant, Resource.AGENT)

    def test_en28_an_unknown_resource_is_allowed_rather_than_refused(self):
        """EN-28: **a quota lookup must not be why creating something fails.**"""
        self.enforce()

        with self.assertLogs("onboard.services.entitlements", level="WARNING"):
            entitlement_service.check(self.tenant, "teleportation")

    def test_en29_an_internal_failure_allows_the_work_and_logs(self):
        """EN-29: fail open. Refusing a paying customer's work because a counting
        query broke is the worse of the two failures."""
        from unittest.mock import patch

        self.enforce()

        with patch.object(
            entitlement_service, "current_usage", side_effect=RuntimeError("boom")
        ):
            with self.assertLogs("onboard.services.entitlements", level="ERROR"):
                self.check()

    def test_en30_is_enforced_reads_the_tenants_flag(self):
        """EN-30: off by default, and per tenant."""
        self.assertFalse(entitlement_service.is_enforced(self.tenant.id))

        self.enforce()

        self.assertTrue(entitlement_service.is_enforced(self.tenant.id))


class SummaryTests(EntitlementTestCase):
    """EN-31 to EN-36 — what the settings page renders."""

    def test_en31_every_resource_appears_exactly_once(self):
        """EN-31: a resource missing from the summary is one the tenant cannot see
        they are near the limit of."""
        rows = entitlement_service.summary(self.tenant.id, self.subscription)

        self.assertEqual(len(rows), len(Resource.values))
        self.assertEqual(
            {row["resource"] for row in rows}, set(Resource.values)
        )

    def test_en32_usage_and_headroom_are_added_on_request(self):
        """EN-32: one query per resource, so it is opt-in — fine for a settings
        page, not for a hot path."""
        make_agent(self.tenant)

        rows = {
            row["resource"]: row
            for row in entitlement_service.summary(
                self.tenant.id, self.subscription, with_usage=True
            )
        }

        self.assertEqual(rows[Resource.AGENT]["used"], 1)
        self.assertEqual(rows[Resource.AGENT]["limit"], 2)
        self.assertEqual(rows[Resource.AGENT]["remaining"], 1)

    def test_en33_headroom_never_goes_negative(self):
        """EN-33: a tenant who is over their limit sees 0 left, not −3 left."""
        for _ in range(5):
            make_agent(self.tenant)

        rows = {
            row["resource"]: row
            for row in entitlement_service.summary(
                self.tenant.id, self.subscription, with_usage=True
            )
        }

        self.assertEqual(rows[Resource.AGENT]["used"], 5)
        self.assertEqual(rows[Resource.AGENT]["remaining"], 0)

    def test_en34_an_unlimited_resource_reports_no_headroom_figure(self):
        """EN-34: ``None``, not a large number — the UI has to be able to render
        'unlimited' rather than '9999 left'."""
        unlimited_plan = make_plan(
            "entitlements-summary-unlimited", with_entitlements=False
        )
        PlanEntitlement.objects.create(
            plan=unlimited_plan, resource=Resource.AGENT,
            included_quantity=None, is_addon_available=False,
        )
        other = make_tenant("entitlements-summary-tenant")
        subscription = subscription_service.activate(other, unlimited_plan)

        rows = {
            row["resource"]: row
            for row in entitlement_service.summary(
                other.tenant.id, subscription, with_usage=True
            )
        }

        self.assertIsNone(rows[Resource.AGENT]["limit"])
        self.assertIsNone(rows[Resource.AGENT]["remaining"])

    def test_en35_the_recurring_total_sums_every_add_on(self):
        """EN-35: **a renewal order is the plan's price plus whatever capacity the
        tenant is still renting.** Missing one here undercharges every cycle."""
        TenantEntitlement.objects.create(
            tenant=self.tenant, resource=Resource.AGENT,
            additional_quantity=3, price_per_unit=Decimal("500.00"), addon_step=1,
        )
        TenantEntitlement.objects.create(
            tenant=self.tenant, resource=Resource.STORAGE_GB,
            additional_quantity=20, price_per_unit=Decimal("199.00"), addon_step=10,
        )

        self.assertEqual(
            entitlement_service.recurring_total(self.tenant.id),
            Decimal("1898.00"),
        )

    def test_en36_a_tenant_with_no_add_ons_adds_nothing(self):
        """EN-36: ``0.00`` rather than ``None``, so a renewal quote can add it
        unconditionally."""
        self.assertEqual(
            entitlement_service.recurring_total(self.tenant.id), Decimal("0.00")
        )
