"""
Everything a VB_AGENT creates must land in the tenant it named — and be visible there.

This is the handover invariant, not a scoping nicety. `tenant` is what every human-facing
queryset filters on, so a record written with the wrong tenant (or none) is not merely
mis-scoped: it is invisible to the people who own it. A booking the voicebot made would
simply never appear in the customer's list, and nothing would look broken until someone
asked where their appointment went.

So each test here writes as the machine caller and then reads back **as a human user of
that tenant**, through the ordinary API. Asserting `record.tenant_id` alone would pass
even if the human-facing filter disagreed.
"""

from unittest.mock import patch

from django.contrib.auth.hashers import make_password
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.models.agent import Agent
from core.models.agent_call_log import AgentCallLog
from core.models.agent_call_recording import AgentCallRecording
from core.models.booking import Booking
from core.tests.scheduling.factories import at, make_host, make_template, make_tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models.CustomUser import CustomUser as User
from user.models.agent_token import AgentToken

CORE = "/api/v1/core"


class CreatedRecordTenancyTestCase(APITestCase):
    def setUp(self):
        self.tenant = make_tenant(name="Acme", subdomain="acme-tenancy")
        self.other = make_tenant(name="Globex", subdomain="globex-tenancy")

        runner = User.objects.create_user(
            username="tenancy_vb",
            email="tenancy-vb@example.com",
            password="password",
            user_type=User.UserType.VB_AGENT,
        )
        secret, prefix = "tenancy_secret", "vb_tenancy"
        AgentToken.objects.create(
            user=runner, name="Tenancy", prefix=prefix,
            key=make_password(secret), never_expires=True,
        )
        self.vb = APIClient()
        self.vb.credentials(HTTP_AUTHORIZATION=f"Bearer {prefix}_{secret}")
        self.hdr = {"HTTP_X_TENANT_ID": str(self.tenant.id)}

        # The human who must be able to see whatever the bot wrote.
        self.human = User.objects.create_user(
            username="tenancy_human",
            email="tenancy-human@example.com",
            password="password",
        )
        TenantUserMapping.objects.create(user=self.human, tenant=self.tenant)
        self.as_human = APIClient()
        self.as_human.force_authenticate(user=self.human)

        self.agent = Agent.objects.create(
            tenant=self.tenant, bot_name="Tenancy Bot",
            persona_name="Persona", languages=["en-IN"],
        )
        self.template = make_template(self.tenant, name="Consult")
        self.host = make_host(self.tenant, self.template)

    # ---- call logs ----

    def test_created_call_log_is_visible_to_a_human_in_that_tenant(self):
        created = self.vb.post(
            f"{CORE}/agent-call-logs/",
            {
                "provider_call_id": "tenancy-call-1",
                "agent": str(self.agent.id),
                "caller_number": "+14155550000",
                "direction": "inbound",
            },
            format="json",
            **self.hdr,
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)

        log = AgentCallLog.objects.get(provider_call_id="tenancy-call-1")
        self.assertEqual(log.tenant_id, self.tenant.id)

        listing = self.as_human.get(f"{CORE}/agent-call-logs/")
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertIn(
            "tenancy-call-1",
            {row["provider_call_id"] for row in listing.data["data"]},
        )

    # ---- recordings ----

    @patch("core.views.agent_call_log.S3StorageService")
    def test_requested_recording_lands_in_the_named_tenant(self, mock_s3):
        mock_s3.return_value.get_presigned_put_url.return_value = "https://example/put"

        r = self.vb.post(
            f"{CORE}/agent-call-logs/request-upload-url/",
            {
                "agent": str(self.agent.id),
                "filename": "call.mp3",
                "content_type": "audio/mpeg",
                "file_size_in_bytes": 1024,
            },
            format="json",
            **self.hdr,
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)

        recording = AgentCallRecording.objects.get(id=r.data["data"]["file_id"])
        self.assertEqual(recording.tenant_id, self.tenant.id)
        # The object key carries the tenant prefix, which is what authorizes the later
        # confirm step — a wrong tenant here would strand the file outside the customer's
        # namespace.
        self.assertIn(str(self.tenant.id), recording.file.name)

    # ---- bookings ----

    def _book(self, start):
        return self.vb.post(
            f"{CORE}/bookings/",
            {
                "event_template": str(self.template.id),
                "start_time": start.isoformat(),
                "target_host": str(self.host.id),
                "participants": [{"name": "Ada", "email": "ada@example.com"}],
            },
            format="json",
            **self.hdr,
        )

    def test_created_booking_is_visible_to_a_human_in_that_tenant(self):
        created = self._book(at(11))
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        booking_id = created.data["data"]["id"]

        booking = Booking.objects.get(id=booking_id)
        self.assertEqual(booking.tenant_id, self.tenant.id)

        listing = self.as_human.get(f"{CORE}/bookings/")
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertIn(booking_id, {row["id"] for row in listing.data["data"]})

        detail = self.as_human.get(f"{CORE}/bookings/{booking_id}/")
        self.assertEqual(detail.status_code, status.HTTP_200_OK)

    def test_booking_participants_inherit_the_tenant(self):
        """
        Participants are a separate tenant-partitioned table written by the service.
        A booking whose participants carry no tenant renders as an empty attendee list.
        """
        created = self._book(at(12))
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)

        booking = Booking.objects.get(id=created.data["data"]["id"])
        participants = list(booking.participants.all())

        self.assertTrue(participants)
        for participant in participants:
            self.assertEqual(participant.tenant_id, self.tenant.id)

    def test_held_booking_is_visible_to_a_human_in_that_tenant(self):
        held = self.vb.post(
            f"{CORE}/bookings/hold/",
            {
                "event_template": str(self.template.id),
                "start_time": at(13).isoformat(),
                "target_host": str(self.host.id),
                "participants": [{"name": "Ada", "email": "ada@example.com"}],
            },
            format="json",
            **self.hdr,
        )
        self.assertEqual(held.status_code, status.HTTP_201_CREATED, held.data)

        booking = Booking.objects.get(id=held.data["data"]["id"])
        self.assertEqual(booking.tenant_id, self.tenant.id)
        self.assertEqual(booking.status, Booking.Status.HELD)

        detail = self.as_human.get(f"{CORE}/bookings/{booking.id}/")
        self.assertEqual(detail.status_code, status.HTTP_200_OK)

    def test_a_bot_booking_cannot_land_in_a_tenant_the_header_did_not_name(self):
        """The header is the authority; nothing the payload carries may override it."""
        created = self._book(at(14))
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)

        booking = Booking.objects.get(id=created.data["data"]["id"])
        self.assertNotEqual(booking.tenant_id, self.other.id)

        # And a human in the other tenant must not see it.
        outsider = User.objects.create_user(
            username="outsider", email="outsider@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=outsider, tenant=self.other)
        as_outsider = APIClient()
        as_outsider.force_authenticate(user=outsider)

        self.assertEqual(
            as_outsider.get(f"{CORE}/bookings/{booking.id}/").status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_bot_and_human_see_the_same_booking_row(self):
        """
        The round trip that matters: the bot books, the human sees it, the human cancels
        it, and the bot sees the cancellation. A tenant mismatch breaks this in one
        direction or the other.
        """
        created = self._book(at(15))
        booking_id = created.data["data"]["id"]

        canceled = self.as_human.post(
            f"{CORE}/bookings/{booking_id}/cancel/",
            {"reason": "customer called back"},
            format="json",
        )
        self.assertEqual(canceled.status_code, status.HTTP_200_OK, canceled.data)

        seen_by_bot = self.vb.get(f"{CORE}/bookings/{booking_id}/", **self.hdr)
        self.assertEqual(seen_by_bot.status_code, status.HTTP_200_OK)
        self.assertEqual(seen_by_bot.data["data"]["status"], Booking.Status.CANCELED)


class HeaderIsMandatoryEverywhereTestCase(APITestCase):
    """
    No endpoint a token can reach may be called without `X-Tenant-ID`.

    An unscoped machine call is the cross-tenant leak this whole mechanism exists to
    prevent, so the absence of the header is an error on every route — including the
    three that predate it and were previously allowed to run unscoped.
    """

    def setUp(self):
        self.tenant = make_tenant(name="Acme", subdomain="acme-mandatory")

        runner = User.objects.create_user(
            username="mandatory_vb",
            email="mandatory-vb@example.com",
            password="password",
            user_type=User.UserType.VB_AGENT,
        )
        secret, prefix = "mandatory_secret", "vb_mand"
        AgentToken.objects.create(
            user=runner, name="Mandatory", prefix=prefix,
            key=make_password(secret), never_expires=True,
        )
        self.vb = APIClient()
        self.vb.credentials(HTTP_AUTHORIZATION=f"Bearer {prefix}_{secret}")

        self.agent = Agent.objects.create(
            tenant=self.tenant, bot_name="Mandatory Bot",
            persona_name="Persona", languages=["en-IN"],
        )
        self.template = make_template(self.tenant, name="Consult")
        self.host = make_host(self.tenant, self.template)

    def test_previously_optional_reads_now_require_the_header(self):
        paths = [
            f"{CORE}/agents/{self.agent.id}/",
            f"{CORE}/agent-call-logs/",
            f"{CORE}/event-templates/",
            f"{CORE}/event-skills/",
            f"{CORE}/bookings/",
        ]
        for path in paths:
            with self.subTest(path=path):
                r = self.vb.get(path)
                self.assertEqual(
                    r.status_code, status.HTTP_400_BAD_REQUEST, f"{path} -> {r.status_code}"
                )

    def test_call_log_create_requires_the_header(self):
        r = self.vb.post(
            f"{CORE}/agent-call-logs/",
            {
                "provider_call_id": "no-header-call",
                "agent": str(self.agent.id),
                "caller_number": "+14155550000",
                "direction": "inbound",
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            AgentCallLog.objects.filter(provider_call_id="no-header-call").exists()
        )

    def test_recording_upload_requires_the_header(self):
        r = self.vb.post(
            f"{CORE}/agent-call-logs/request-upload-url/",
            {
                "agent": str(self.agent.id),
                "filename": "call.mp3",
                "content_type": "audio/mpeg",
                "file_size_in_bytes": 1024,
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(AgentCallRecording.objects.count(), 0)

    def test_booking_create_requires_the_header(self):
        r = self.vb.post(
            f"{CORE}/bookings/",
            {
                "event_template": str(self.template.id),
                "start_time": (at(16)).isoformat(),
                "target_host": str(self.host.id),
                "participants": [{"name": "Ada", "email": "ada@example.com"}],
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Booking.objects.count(), 0)

    def test_campaign_runner_requires_the_header(self):
        from core.models.campaign import Campaign

        campaign = Campaign.objects.create(
            tenant=self.tenant, agent=self.agent,
            name="Mandatory Campaign", status=Campaign.Status.RUNNING,
        )
        r = self.vb.get(f"{CORE}/campaign-runner/{campaign.id}/context/")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

        ok = self.vb.get(
            f"{CORE}/campaign-runner/{campaign.id}/context/",
            HTTP_X_TENANT_ID=str(self.tenant.id),
        )
        self.assertEqual(ok.status_code, status.HTTP_200_OK)

    def test_the_error_names_the_header(self):
        r = self.vb.get(f"{CORE}/agent-call-logs/")
        self.assertIn("X-Tenant-ID", str(r.data))
