"""
The full VB_AGENT access matrix.

Machine callers are closed by default: `VBAgentActionPermission` denies any viewset that
does not declare `vb_agent_allowed_actions`. These tests pin both halves — what is open,
and that the rest stayed shut — because the surface previously grew silently. Every new
viewset admitted tokens the moment it was written, since `AgentTokenAuthentication` is a
global auth class and `IsAuthenticated` was the only gate.

Endpoints granted here take their tenant from `X-Tenant-ID`; a machine caller has no
tenant mapping, so a booking has nowhere to land without it.
"""

from datetime import timedelta

from django.contrib.auth.hashers import make_password
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.models.agent import Agent
from core.models.availability_profile import AvailabilityProfile
from core.models.booking import Booking
from core.models.campaign import Campaign
from core.models.event_template import EventTemplate
from core.tests.scheduling.factories import (
    at,
    make_host,
    make_template,
    make_tenant,
)
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models.CustomUser import CustomUser as User
from user.models.agent_token import AgentToken

CORE = "/api/v1/core"


class VBAgentAccessMatrixTestCase(APITestCase):
    def setUp(self):
        self.tenant = make_tenant(name="Acme", subdomain="acme-matrix")
        self.other = make_tenant(name="Globex", subdomain="globex-matrix")

        runner = User.objects.create_user(
            username="matrix_vb",
            email="matrix-vb@example.com",
            password="password",
            user_type=User.UserType.VB_AGENT,
        )
        secret, prefix = "matrix_secret", "vb_matrix"
        AgentToken.objects.create(
            user=runner, name="Matrix", prefix=prefix,
            key=make_password(secret), never_expires=True,
        )
        self.vb = APIClient()
        self.vb.credentials(HTTP_AUTHORIZATION=f"Bearer {prefix}_{secret}")
        self.hdr = {"HTTP_X_TENANT_ID": str(self.tenant.id)}

        self.agent = Agent.objects.create(
            tenant=self.tenant, bot_name="Matrix Bot",
            persona_name="Persona", languages=["en-IN"],
        )
        self.campaign = Campaign.objects.create(
            tenant=self.tenant, agent=self.agent,
            name="Matrix Campaign", status=Campaign.Status.RUNNING,
        )

        # make_host gives the user a tenant mapping, the skill for this template, and
        # 24/7 availability, so slot lookups do not depend on wall-clock time.
        self.template = make_template(self.tenant, name="Consult")
        self.host = make_host(self.tenant, self.template)
        self.profile = AvailabilityProfile.objects.get(user=self.host)

        start = at(10)
        self.booking = Booking.objects.create(
            tenant=self.tenant,
            event_template=self.template,
            title="Existing",
            primary_host=self.host,
            start_time=start,
            end_time=start + timedelta(minutes=30),
            effective_start_time=start,
            effective_end_time=start + timedelta(minutes=30),
            status=Booking.Status.SCHEDULED,
        )

    # ---- agent: retrieve only ----

    def test_agent_retrieve_is_allowed(self):
        r = self.vb.get(f"{CORE}/agents/{self.agent.id}/", **self.hdr)
        self.assertEqual(r.status_code, status.HTTP_200_OK)

    def test_agent_list_is_denied(self):
        r = self.vb.get(f"{CORE}/agents/", **self.hdr)
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_agent_dropdown_is_denied(self):
        r = self.vb.get(f"{CORE}/agents/dropdown/", **self.hdr)
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_agent_create_is_denied(self):
        r = self.vb.post(f"{CORE}/agents/", {"bot_name": "X"}, format="json", **self.hdr)
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    # ---- agent call log: full ----

    def test_call_log_list_and_create_are_allowed(self):
        listing = self.vb.get(f"{CORE}/agent-call-logs/", **self.hdr)
        self.assertEqual(listing.status_code, status.HTTP_200_OK)

        created = self.vb.post(
            f"{CORE}/agent-call-logs/",
            {
                "provider_call_id": "matrix-call-1",
                "agent": str(self.agent.id),
                "caller_number": "+14155550000",
                "direction": "inbound",
            },
            format="json",
            **self.hdr,
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)

    # ---- campaign: retrieve only; runner unchanged ----

    def test_campaign_retrieve_is_allowed(self):
        r = self.vb.get(f"{CORE}/campaigns/{self.campaign.id}/", **self.hdr)
        self.assertEqual(r.status_code, status.HTTP_200_OK)

    def test_campaign_list_is_denied(self):
        r = self.vb.get(f"{CORE}/campaigns/", **self.hdr)
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_campaign_launch_is_denied(self):
        r = self.vb.post(f"{CORE}/campaigns/{self.campaign.id}/launch/", **self.hdr)
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_campaign_retrieve_requires_the_header(self):
        r = self.vb.get(f"{CORE}/campaigns/{self.campaign.id}/")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_campaign_retrieve_is_scoped_to_the_named_tenant(self):
        r = self.vb.get(
            f"{CORE}/campaigns/{self.campaign.id}/",
            HTTP_X_TENANT_ID=str(self.other.id),
        )
        self.assertEqual(r.status_code, status.HTTP_404_NOT_FOUND)

    def test_campaign_runner_still_works(self):
        r = self.vb.get(f"{CORE}/campaign-runner/{self.campaign.id}/context/", **self.hdr)
        self.assertEqual(r.status_code, status.HTTP_200_OK)

    # ---- bookings: full ----

    def test_booking_list_and_retrieve_are_allowed(self):
        listing = self.vb.get(f"{CORE}/bookings/", **self.hdr)
        self.assertEqual(listing.status_code, status.HTTP_200_OK)

        detail = self.vb.get(f"{CORE}/bookings/{self.booking.id}/", **self.hdr)
        self.assertEqual(detail.status_code, status.HTTP_200_OK)

    def test_booking_cancel_is_allowed(self):
        r = self.vb.post(
            f"{CORE}/bookings/{self.booking.id}/cancel/",
            {"reason": "caller rescheduled"},
            format="json",
            **self.hdr,
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CANCELED)

    def test_booking_list_requires_the_header(self):
        r = self.vb.get(f"{CORE}/bookings/")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_booking_is_scoped_to_the_named_tenant(self):
        r = self.vb.get(
            f"{CORE}/bookings/{self.booking.id}/",
            HTTP_X_TENANT_ID=str(self.other.id),
        )
        self.assertEqual(r.status_code, status.HTTP_404_NOT_FOUND)

    def test_booking_create_is_allowed(self):
        r = self.vb.post(
            f"{CORE}/bookings/",
            {
                "event_template": str(self.template.id),
                "start_time": at(14).isoformat(),
                "target_host": str(self.host.id),
                "participants": [{"name": "Ada", "email": "ada@example.com"}],
            },
            format="json",
            **self.hdr,
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.assertEqual(
            Booking.objects.get(id=r.data["data"]["id"]).tenant_id, self.tenant.id
        )

    def test_booking_create_cannot_use_another_tenants_event_template(self):
        foreign = make_template(self.other, name="Foreign")
        r = self.vb.post(
            f"{CORE}/bookings/",
            {
                "event_template": str(foreign.id),
                "start_time": at(15).isoformat(),
                "target_host": str(self.host.id),
                "participants": [{"name": "Ada", "email": "ada@example.com"}],
            },
            format="json",
            **self.hdr,
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    # ---- availability + read-only booking config ----

    def _slots_query(self):
        start = at(0)
        return {
            "event_template": str(self.template.id),
            "start_utc": start.isoformat(),
            "end_utc": (start + timedelta(days=2)).isoformat(),
            "target_host": str(self.host.id),
        }

    def test_available_slots_is_allowed(self):
        r = self.vb.get(
            f"{CORE}/availability/available-slots/", self._slots_query(), **self.hdr
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)

    def test_available_slots_requires_the_header(self):
        r = self.vb.get(f"{CORE}/availability/available-slots/", self._slots_query())
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_available_slots_rejects_another_tenants_template(self):
        # The header says Globex; the template is Acme's. The tenant must come from the
        # header and then be checked, never derived from the template the caller named.
        r = self.vb.get(
            f"{CORE}/availability/available-slots/",
            self._slots_query(),
            HTTP_X_TENANT_ID=str(self.other.id),
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_event_templates_are_readable(self):
        r = self.vb.get(f"{CORE}/event-templates/", **self.hdr)
        self.assertEqual(r.status_code, status.HTTP_200_OK)

    def test_event_templates_are_not_writable(self):
        r = self.vb.post(
            f"{CORE}/event-templates/", {"name": "New"}, format="json", **self.hdr
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_event_skills_are_readable_but_not_writable(self):
        self.assertEqual(
            self.vb.get(f"{CORE}/event-skills/", **self.hdr).status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(
            self.vb.post(f"{CORE}/event-skills/", {}, format="json", **self.hdr).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    # ---- everything else stays shut ----

    def test_configuration_and_unrelated_endpoints_are_denied(self):
        denied = [
            f"{CORE}/availability-profiles/",
            f"{CORE}/availability-exceptions/",
            f"{CORE}/calendar-integrations/",
            f"{CORE}/contacts/",
            f"{CORE}/contact-imports/",
            f"{CORE}/knowledge-base/",
            f"{CORE}/agent-templates/",
            f"{CORE}/probing-questions/",
            f"{CORE}/custom-voices/",
            f"{CORE}/device-tokens/",
            "/api/v1/auth/users/",
            "/api/v1/auth/sessions/",
        ]
        for path in denied:
            with self.subTest(path=path):
                r = self.vb.get(path, **self.hdr)
                self.assertEqual(
                    r.status_code, status.HTTP_403_FORBIDDEN, f"{path} -> {r.status_code}"
                )

    def test_availability_profile_write_is_denied(self):
        r = self.vb.patch(
            f"{CORE}/availability-profiles/{self.profile.id}/",
            {"is_active": False},
            format="json",
            **self.hdr,
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)


class HumansAreUnaffectedTestCase(APITestCase):
    """The permission only ever narrows VB_AGENT; a mapped user must see no change."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-human")
        self.user = User.objects.create_user(
            username="human_matrix",
            email="human-matrix@example.com",
            password="password",
        )
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)
        self.client_human = APIClient()
        self.client_human.force_authenticate(user=self.user)

        self.agent = Agent.objects.create(
            tenant=self.tenant, bot_name="Human Bot",
            persona_name="Persona", languages=["en-IN"],
        )

    def test_human_keeps_full_agent_access(self):
        self.assertEqual(
            self.client_human.get(f"{CORE}/agents/").status_code, status.HTTP_200_OK
        )
        self.assertEqual(
            self.client_human.get(f"{CORE}/agents/dropdown/").status_code,
            status.HTTP_200_OK,
        )

    def test_human_keeps_config_access(self):
        for path in (
            f"{CORE}/event-templates/",
            f"{CORE}/availability-profiles/",
            f"{CORE}/contacts/",
            "/api/v1/auth/sessions/",
        ):
            with self.subTest(path=path):
                r = self.client_human.get(path)
                self.assertEqual(r.status_code, status.HTTP_200_OK, path)

    def test_human_header_is_ignored(self):
        other = Tenant.objects.create(name="Globex", subdomain="globex-human")
        r = self.client_human.get(
            f"{CORE}/agents/", HTTP_X_TENANT_ID=str(other.id)
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        ids = {row["id"] for row in r.data["data"]}
        self.assertEqual(ids, {str(self.agent.id)})
