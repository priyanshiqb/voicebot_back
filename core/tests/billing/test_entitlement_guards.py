"""Section 20 of the billing test plan — EG-01 to EG-24.

Plan limits, enforced at the point of creation.

Two design decisions are on trial throughout. **402, not 403** — 403 means "you may
not do this", whereas this is "you may, but you have used up what you bought", a
billing state the tenant can resolve themselves. And **the guard fails open**: a quota
lookup must never be why creating an agent fails, so an error in the check allows the
create and logs (EG-22).

EG-21 is the one that found a real gap. ``EntitlementGuardMixin`` hooks
``perform_create``, and a viewset that overrides it without calling ``super()``
silently loses the guard — the endpoint keeps working and simply stops enforcing.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework import status

from core.models.agent import Agent
from core.models.campaign import Campaign
from core.models.contact import Contact
from core.models.knowledge_base import KBWebsite, KnowledgeBase
from onboard.constants.entitlement_resource import EntitlementResource
from onboard.models import PlanEntitlement, TenantEntitlement
from onboard.services import entitlements as entitlement_service
from onboard.services import subscriptions as subscription_service
from onboard.tests.billing_factories import make_agent, make_plan, make_tenant
from tenant.models import TenantSettings, TenantUserMapping

User = get_user_model()
Resource = EntitlementResource

def unique_suffix():
    """Digits only. Bot and campaign names are validated against ^[\\w\\s-]+$, so a
    timestamp with a decimal point in it fails as a 400 long before any quota runs."""
    return str(timezone.now().timestamp()).replace(".", "")


def quota_body(response):
    """The refusal payload, whichever layer wrapped it.

    The project exception handler nests an APIException detail under ``errors``, so
    a guard refusal arrives one level deeper than the view wrote it.
    """
    body = response.json()
    inner = body.get("errors")
    return inner if isinstance(inner, dict) else body


CORE = "/api/v1/core"
AGENTS_URL = f"{CORE}/agents/"
CONTACTS_URL = f"{CORE}/contacts/"
KB_URL = f"{CORE}/knowledge-base/"
CAMPAIGNS_URL = f"{CORE}/campaigns/"
USERS_URL = "/api/v1/auth/users/"


class GuardTestCase(TestCase):
    """A tenant on a plan with deliberately small allowances.

    The external ETL call a knowledge base makes on save is stubbed for the whole
    class: it is a four-second network timeout per create, and it has nothing to do
    with quotas.
    """

    def setUp(self):
        self.etl = patch("core.models.knowledge_base.requests.post")
        self.etl.start()
        self.addCleanup(self.etl.stop)

        self.ctx = make_tenant("guards")
        self.tenant = self.ctx.tenant
        self.client = self.ctx.client
        self.plan = make_plan("guards-plan")
        self.subscription = subscription_service.activate(self.tenant, self.plan)

    # --- helpers ------------------------------------------------------------

    def enforce(self, tenant=None, enabled=True):
        settings_row = TenantSettings.for_tenant((tenant or self.tenant).id)
        settings_row.entitlements_enforced = enabled
        settings_row.save(update_fields=["entitlements_enforced"])
        return settings_row

    def create_agent(self, client=None, name="Guarded Bot"):
        return (client or self.client).post(
            AGENTS_URL,
            {"bot_name": f"{name} {unique_suffix()}",
             "persona_name": "Guarded Persona"},
            format="json",
        )

    def create_contact(self, client=None, phone=None):
        stamp = unique_suffix()[-8:]
        return (client or self.client).post(
            CONTACTS_URL,
            {"first_name": "Guarded", "last_name": "Contact",
             "phone_number": phone or f"+9198{stamp}", "phone_region": "IN"},
            format="json",
        )

    def create_kb(self, websites=0, client=None):
        payload = {"name": f"Guarded KB {unique_suffix()}"}
        if websites:
            stamp = unique_suffix()
            payload["websites"] = [
                {"url": f"https://example.com/{stamp}/{index}"}
                for index in range(websites)
            ]
        return (client or self.client).post(KB_URL, payload, format="json")

    def create_campaign(self, client=None, agent=None):
        return (client or self.client).post(
            CAMPAIGNS_URL,
            {
                "name": f"Guarded Campaign {unique_suffix()}",
                "agent": str((agent or make_agent(self.tenant)).id),
                "audience_source": Campaign.AudienceSource.SELECTED,
            },
            format="json",
        )

    def create_user(self, client=None):
        stamp = unique_suffix()
        return (client or self.client).post(
            USERS_URL,
            {"email": f"seat-{stamp}@example.com", "username": f"seat-{stamp}",
             "password": "Sup3rSecret!42", "first_name": "Seat",
             "last_name": "Holder"},
            format="json",
        )

    def fill(self, resource, count):
        """Take a resource up to ``count`` without going through the API."""
        for _ in range(count):
            if resource == Resource.AGENT:
                make_agent(self.tenant)


class AgentGuardTests(GuardTestCase):
    """EG-01 to EG-05, EG-22, EG-23 — the guard's core behaviour."""

    def test_eg01_creating_under_the_limit_succeeds(self):
        """EG-01."""
        self.enforce()

        self.assertEqual(
            self.create_agent().status_code, status.HTTP_201_CREATED
        )

    def test_eg02_creating_over_the_limit_is_refused(self):
        """EG-02: two agents included, so the third is over."""
        self.enforce()
        self.fill(Resource.AGENT, 2)

        response = self.create_agent()

        self.assertEqual(response.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        self.assertIn("QUOTA_EXCEEDED", quota_body(response)["errors"])

    def test_eg03_going_over_while_enforcement_is_off_is_allowed_and_logged(self):
        """EG-03: **switching caps on for accounts already over them would break
        working setups with no warning**, so the default is to count and report."""
        self.fill(Resource.AGENT, 5)

        with self.assertLogs("onboard.services.entitlements", level="WARNING"):
            response = self.create_agent()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_eg04_the_refusal_says_what_was_used_and_what_the_limit_is(self):
        """EG-04: "2 of 2 agents" tells someone what to do next; "forbidden" does
        not."""
        self.enforce()
        self.fill(Resource.AGENT, 2)

        body = quota_body(self.create_agent())

        self.assertEqual(body["data"]["resource"], Resource.AGENT)
        self.assertEqual(int(body["data"]["used"]), 2)
        self.assertEqual(int(body["data"]["limit"]), 2)
        self.assertIn("Upgrade", body["message"])

    def test_eg05_nothing_is_created_when_the_guard_refuses(self):
        """EG-05: checked in ``perform_create``, before the row is written."""
        self.enforce()
        self.fill(Resource.AGENT, 2)

        self.create_agent()

        self.assertEqual(Agent.objects.filter(tenant=self.tenant).count(), 2)

    def test_eg22_a_broken_guard_never_blocks_a_create(self):
        """EG-22: **a quota lookup must not be why creating an agent fails.**"""
        self.enforce()

        with patch.object(
            entitlement_service, "current_usage",
            side_effect=RuntimeError("counting query broke"),
        ):
            with self.assertLogs("onboard.services.entitlements", level="ERROR"):
                response = self.create_agent()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_eg23_the_status_is_payment_required_not_forbidden(self):
        """EG-23: **the same code the wallet uses when credits run out**, so the
        frontend has one thing to handle for "you need to pay for more"."""
        self.enforce()
        self.fill(Resource.AGENT, 2)

        response = self.create_agent()

        self.assertEqual(response.status_code, 402)
        self.assertNotEqual(response.status_code, 403)


class ResourceCoverageTests(GuardTestCase):
    """EG-06 to EG-12, EG-24 — every guarded endpoint behaves the same way."""

    def test_eg06_contacts_are_guarded(self):
        """EG-06: 100 included."""
        self.enforce()
        self.plan.entitlements.filter(resource=Resource.CONTACT).update(
            included_quantity=1
        )
        self.subscription.generate_plan_snapshot()
        self.subscription.save(update_fields=["plan_snapshot"])
        self.assertEqual(self.create_contact().status_code, status.HTTP_201_CREATED)

        response = self.create_contact()

        self.assertEqual(response.status_code, status.HTTP_402_PAYMENT_REQUIRED)

    def test_eg07_knowledge_bases_are_guarded(self):
        """EG-07: 2 included."""
        self.enforce()
        self.create_kb()
        self.create_kb()

        response = self.create_kb()

        self.assertEqual(response.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        self.assertEqual(
            KnowledgeBase.objects.filter(tenant=self.tenant).count(), 2
        )

    def test_eg08_campaigns_are_guarded(self):
        """EG-08: 5 a month included."""
        self.enforce()
        agent = make_agent(self.tenant)
        for _ in range(5):
            self.assertEqual(
                self.create_campaign(agent=agent).status_code,
                status.HTTP_201_CREATED,
            )

        response = self.create_campaign(agent=agent)

        self.assertEqual(response.status_code, status.HTTP_402_PAYMENT_REQUIRED)

    def test_eg09_seats_are_guarded(self):
        """EG-09: three seats included, and the tenant already holds one — so the
        fourth user is the one refused."""
        self.enforce()
        self.assertEqual(self.create_user().status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.create_user().status_code, status.HTTP_201_CREATED)

        response = self.create_user()

        self.assertEqual(response.status_code, status.HTTP_402_PAYMENT_REQUIRED)

    def test_eg10_a_knowledge_base_consumes_a_crawl_page_per_website(self):
        """EG-10: **checking only the container would let a tenant hold any number
        of crawled pages by putting them all in one knowledge base.**"""
        self.enforce()

        response = self.create_kb(websites=3)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            entitlement_service.current_usage(self.tenant.id, Resource.CRAWL_PAGE),
            3,
        )

    def test_eg11_a_knowledge_base_over_the_page_limit_creates_nothing(self):
        """EG-11: both resources are checked before either is written, so a refusal
        leaves neither the container nor the pages behind."""
        self.enforce()
        self.plan.entitlements.filter(resource=Resource.CRAWL_PAGE).update(
            included_quantity=2
        )
        self.subscription.generate_plan_snapshot()
        self.subscription.save(update_fields=["plan_snapshot"])

        response = self.create_kb(websites=5)

        self.assertEqual(response.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        self.assertEqual(KnowledgeBase.objects.filter(tenant=self.tenant).count(), 0)
        self.assertEqual(KBWebsite.objects.filter(tenant=self.tenant).count(), 0)

    def test_eg12_a_knowledge_base_with_no_websites_checks_only_itself(self):
        """EG-12: a zero-count request is skipped rather than checked, so an
        exhausted page allowance does not block an empty knowledge base."""
        self.enforce()
        self.plan.entitlements.filter(resource=Resource.CRAWL_PAGE).update(
            included_quantity=0
        )
        self.subscription.generate_plan_snapshot()
        self.subscription.save(update_fields=["plan_snapshot"])

        self.assertEqual(self.create_kb().status_code, status.HTTP_201_CREATED)

    def test_eg24_a_previous_windows_campaigns_do_not_count(self):
        """EG-24: **the one windowed resource.**

        The allowance refills on the subscription's own clock, so campaigns from the
        previous window must not consume this one's.
        """
        from onboard.models import Subscription

        self.enforce()
        Subscription.objects.filter(pk=self.subscription.pk).update(
            created_at=timezone.now() - timedelta(days=20)
        )
        agent = make_agent(self.tenant)
        for _ in range(5):
            self.create_campaign(agent=agent)
        stale = Campaign.objects.filter(tenant=self.tenant)
        Campaign.objects.filter(pk__in=stale.values("pk")).update(
            created_at=timezone.now() - timedelta(days=45)
        )

        response = self.create_campaign(agent=agent)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class LimitInteractionTests(GuardTestCase):
    """EG-13 to EG-20 — how the limit itself moves."""

    def test_eg13_buying_capacity_raises_the_ceiling(self):
        """EG-13: the whole purpose of an add-on."""
        self.enforce()
        self.fill(Resource.AGENT, 2)
        self.assertEqual(
            self.create_agent().status_code, status.HTTP_402_PAYMENT_REQUIRED
        )

        entitlement_service.apply_addon(
            tenant_id=self.tenant.id, subscription=self.subscription,
            resource=Resource.AGENT, additional_units=2,
        )

        self.assertEqual(self.create_agent().status_code, status.HTTP_201_CREATED)

    def test_eg14_an_unlimited_resource_is_never_refused(self):
        """EG-14."""
        unlimited_plan = make_plan("guards-unlimited", with_entitlements=False)
        PlanEntitlement.objects.create(
            plan=unlimited_plan, resource=Resource.AGENT,
            included_quantity=None, is_addon_available=False,
        )
        other = make_tenant("guards-unlimited-tenant")
        subscription_service.activate(other, unlimited_plan)
        self.enforce(other.tenant)
        for _ in range(6):
            make_agent(other.tenant)

        self.assertEqual(
            self.create_agent(client=other.client).status_code,
            status.HTTP_201_CREATED,
        )

    def test_eg15_a_tenant_with_no_plan_is_not_refused_by_the_guard(self):
        """EG-15: **no live plan means no agreed limits.** Whatever gates access in
        that state is the subscription's business, not the quota checker's."""
        stranger = make_tenant("guards-no-plan")
        self.enforce(stranger.tenant)
        for _ in range(10):
            make_agent(stranger.tenant)

        self.assertEqual(
            self.create_agent(client=stranger.client).status_code,
            status.HTTP_201_CREATED,
        )

    def test_eg16_a_caller_with_no_tenant_mapping_is_not_refused_by_the_guard(self):
        """EG-16: tenant scoping has already had its say by then; the guard has no
        plan to check and must not invent one."""
        from rest_framework.test import APIClient

        superuser = User.objects.create_superuser(
            email="guards-super@example.com", username="guards-super",
            password="x",
        )
        client = APIClient()
        client.force_authenticate(user=superuser)
        self.enforce()
        self.fill(Resource.AGENT, 5)

        response = self.create_agent(client=client)

        self.assertNotEqual(response.status_code, status.HTTP_402_PAYMENT_REQUIRED)

    def test_eg17_one_tenants_usage_does_not_refuse_another(self):
        """EG-17."""
        other = make_tenant("guards-isolation")
        subscription_service.activate(other, self.plan)
        self.enforce()
        self.enforce(other.tenant)
        self.fill(Resource.AGENT, 2)

        self.assertEqual(
            self.create_agent(client=other.client).status_code,
            status.HTTP_201_CREATED,
        )

    def test_eg18_updates_are_never_blocked(self):
        """EG-18: **an over-limit tenant must still be able to fix their setup.**

        Blocking edits would leave someone unable to rename or reconfigure the very
        things they need to clean up.
        """
        self.enforce()
        agent = make_agent(self.tenant)
        make_agent(self.tenant)
        make_agent(self.tenant)

        response = self.client.patch(
            f"{AGENTS_URL}{agent.id}/", {"bot_name": "Renamed"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_eg19_deletes_are_never_blocked(self):
        """EG-19: refusing a delete because the tenant is over their limit would
        make the limit impossible to get back under."""
        self.enforce()
        agent = make_agent(self.tenant)
        make_agent(self.tenant)
        make_agent(self.tenant)

        response = self.client.delete(f"{AGENTS_URL}{agent.id}/")

        self.assertIn(
            response.status_code,
            (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT),
        )

    def test_eg20_freeing_a_slot_makes_room_immediately(self):
        """EG-20: **the gauge property.** Counted live from the rows that *are* the
        resource, so there is no counter to reset and no lag."""
        self.enforce()
        agent = make_agent(self.tenant)
        make_agent(self.tenant)
        self.assertEqual(
            self.create_agent().status_code, status.HTTP_402_PAYMENT_REQUIRED
        )

        self.client.delete(f"{AGENTS_URL}{agent.id}/")

        self.assertEqual(self.create_agent().status_code, status.HTTP_201_CREATED)


class ShadowedGuardTests(GuardTestCase):
    """EG-21 — the viewset that overrides ``perform_create``."""

    def test_eg21_the_seat_guard_fires_despite_the_viewsets_own_perform_create(self):
        """EG-21: **the gap that would be invisible.**

        ``CustomUserViewSet.perform_create`` does not call ``super()``, so the
        mixin's hook never runs and the seat limit silently stopped being enforced.
        It calls ``check_entitlement`` explicitly instead — and this is the test
        that says so, because nothing about the endpoint's behaviour would reveal
        it otherwise.
        """
        from user.views.custom_user import CustomUserViewSet

        # The hazard, stated: the viewset really does define its own hook.
        self.assertIn("perform_create", CustomUserViewSet.__dict__)

        self.enforce()
        self.create_user()
        self.create_user()

        response = self.create_user()

        self.assertEqual(response.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        self.assertIn("QUOTA_EXCEEDED", quota_body(response)["errors"])
        self.assertEqual(
            TenantUserMapping.objects.filter(tenant=self.tenant).count(), 3
        )
