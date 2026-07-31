"""
Runner-facing campaign endpoints.

Two invariants carry the weight here: suppressed members must never leave this service,
and updates must be safely retryable. Everything else is plumbing.
"""

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.models.agent import Agent
from core.models.agent_call_log import AgentCallLog
from core.models.campaign import Campaign
from core.models.campaign_member import CampaignMember
from core.models.contact import Contact
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models import CustomUser as User

BASE = "/api/v1/core/campaign-runner/"


class RunnerTestCase(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-runner")

        self.runner = User.objects.create_user(
            username="runner",
            email="runner@example.com",
            password="password",
            user_type=User.UserType.VB_AGENT,
        )
        self.human = User.objects.create_user(
            username="alex", email="alex@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=self.human, tenant=self.tenant)

        self.client_runner = APIClient()
        self.client_runner.force_authenticate(user=self.runner)
        # X-Tenant-ID is mandatory on every endpoint an agent token can reach; without
        # it these requests are a 400 regardless of what they are testing.
        self.client_runner.credentials(HTTP_X_TENANT_ID=str(self.tenant.id))
        self.client_human = APIClient()
        self.client_human.force_authenticate(user=self.human)

        self.agent = Agent.objects.create(
            tenant=self.tenant,
            bot_name="Sales Bot",
            persona_name="Persona",
            languages=["en-IN"],
        )
        self.campaign = Campaign.objects.create(
            tenant=self.tenant,
            agent=self.agent,
            name="Runner Campaign",
            status=Campaign.Status.RUNNING,
        )

        self.pending = self._member("+14155552671", first_name="Ada")
        self.suppressed = self._member(
            "+14155552672",
            first_name="Blocked",
            do_not_call=True,
            member_status=CampaignMember.Status.SUPPRESSED,
            reason=CampaignMember.SuppressionReason.DO_NOT_CALL,
        )
        self.campaign.recompute_counts()

    def _member(
        self,
        phone,
        first_name="",
        member_status=CampaignMember.Status.PENDING,
        reason="",
        **contact_kwargs,
    ):
        contact = Contact.objects.create(
            tenant=self.tenant,
            phone_number=phone,
            first_name=first_name,
            **contact_kwargs,
        )
        return CampaignMember.objects.create(
            tenant=self.tenant,
            campaign=self.campaign,
            contact=contact,
            phone_e164=contact.phone_e164,
            status=member_status,
            suppression_reason=reason,
        )

    def _context(self):
        return self.client_runner.get(f"{BASE}{self.campaign.id}/context/")

    def _update(self, payload, client=None):
        return (client or self.client_runner).post(
            f"{BASE}{self.campaign.id}/members/update/", payload, format="json"
        )


class RunnerAuthTests(RunnerTestCase):
    def test_vb_agent_may_read_context(self):
        self.assertEqual(self._context().status_code, status.HTTP_200_OK)

    def test_ordinary_user_is_refused(self):
        response = self.client_human.get(f"{BASE}{self.campaign.id}/context/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_is_refused(self):
        response = APIClient().get(f"{BASE}{self.campaign.id}/context/")
        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_ordinary_user_cannot_post_updates(self):
        response = self._update(
            {"updates": [{"member_id": str(self.pending.id), "status": "COMPLETED"}]},
            client=self.client_human,
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class RunnerContextTests(RunnerTestCase):
    def test_context_excludes_suppressed_members(self):
        """The single most important assertion in this file."""
        response = self._context()
        ids = {row["id"] for row in response.data["data"]["members"]}
        self.assertIn(str(self.pending.id), ids)
        self.assertNotIn(str(self.suppressed.id), ids)

    def test_context_includes_bot_config_inline(self):
        agent = self._context().data["data"]["campaign"]["agent"]
        self.assertEqual(agent["bot_name"], "Sales Bot")
        self.assertEqual(agent["persona_name"], "Persona")
        self.assertEqual(agent["languages"], ["en-IN"])

    def test_context_reports_counts(self):
        campaign = self._context().data["data"]["campaign"]
        self.assertEqual(campaign["total_members"], 2)
        self.assertEqual(campaign["pending_count"], 1)
        self.assertEqual(campaign["suppressed_count"], 1)

    def test_members_carry_the_id_used_for_updates(self):
        row = self._context().data["data"]["members"][0]
        self.assertEqual(row["id"], str(self.pending.id))
        self.assertEqual(row["phone_e164"], "+14155552671")
        self.assertEqual(row["full_name"], "Ada")

    def test_first_pull_acknowledges_the_handoff(self):
        self.assertEqual(
            self.campaign.handoff_status, Campaign.HandoffStatus.PENDING
        )
        self._context()
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.handoff_status, Campaign.HandoffStatus.ACKED)
        self.assertIsNotNone(self.campaign.handed_off_at)

    def test_second_pull_does_not_move_the_ack_timestamp(self):
        self._context()
        self.campaign.refresh_from_db()
        first = self.campaign.handed_off_at

        self._context()
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.handed_off_at, first)

    def test_draft_campaign_has_nothing_to_run(self):
        self.campaign.status = Campaign.Status.DRAFT
        self.campaign.save(update_fields=["status"])
        self.assertEqual(self._context().status_code, status.HTTP_409_CONFLICT)

    def test_cancelled_campaign_has_nothing_to_run(self):
        self.campaign.status = Campaign.Status.CANCELLED
        self.campaign.save(update_fields=["status"])
        self.assertEqual(self._context().status_code, status.HTTP_409_CONFLICT)

    def test_unknown_campaign_is_404(self):
        import uuid

        response = self.client_runner.get(f"{BASE}{uuid.uuid4()}/context/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class RunnerUpdateTests(RunnerTestCase):
    def test_completed_update_is_applied(self):
        response = self._update(
            {
                "updates": [
                    {
                        "member_id": str(self.pending.id),
                        "status": "COMPLETED",
                        "outcome": "answered",
                    }
                ]
            }
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["applied"], 1)

        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, CampaignMember.Status.COMPLETED)
        self.assertEqual(self.pending.outcome, "answered")
        self.assertEqual(self.pending.attempt_count, 1)
        self.assertIsNotNone(self.pending.last_call_at)

    def test_completed_update_writes_last_contacted_at(self):
        """The field has existed unwritten since the contact module shipped."""
        self.assertIsNone(self.pending.contact.last_contacted_at)

        self._update(
            {"updates": [{"member_id": str(self.pending.id), "status": "COMPLETED"}]}
        )

        self.pending.contact.refresh_from_db()
        self.assertIsNotNone(self.pending.contact.last_contacted_at)

    def test_in_progress_does_not_count_an_attempt(self):
        self._update(
            {"updates": [{"member_id": str(self.pending.id), "status": "IN_PROGRESS"}]}
        )
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, CampaignMember.Status.IN_PROGRESS)
        self.assertEqual(self.pending.attempt_count, 0)

    def test_in_progress_then_failed_counts_one_attempt(self):
        self._update(
            {"updates": [{"member_id": str(self.pending.id), "status": "IN_PROGRESS"}]}
        )
        self._update(
            {"updates": [{"member_id": str(self.pending.id), "status": "FAILED"}]}
        )
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.attempt_count, 1)

    def test_repeated_update_is_ignored_not_double_counted(self):
        """Retry safety: a resent batch must not move a terminal member again."""
        payload = {
            "updates": [{"member_id": str(self.pending.id), "status": "COMPLETED"}]
        }
        self._update(payload)
        response = self._update(payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["applied"], 0)
        self.assertEqual(response.data["data"]["results"][0]["result"], "ignored")

        self.pending.refresh_from_db()
        self.assertEqual(self.pending.attempt_count, 1)

    def test_late_in_progress_after_completed_is_discarded(self):
        """Out-of-order delivery must not walk a member backwards."""
        self._update(
            {"updates": [{"member_id": str(self.pending.id), "status": "COMPLETED"}]}
        )
        self._update(
            {"updates": [{"member_id": str(self.pending.id), "status": "IN_PROGRESS"}]}
        )
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, CampaignMember.Status.COMPLETED)

    def test_runner_cannot_unsuppress_a_member(self):
        """
        The guarantee the whole module exists for: an external service must not be able
        to move somebody who opted out back into the callable set.
        """
        for forbidden in ["PENDING", "SUPPRESSED", "CANCELLED"]:
            with self.subTest(status=forbidden):
                response = self._update(
                    {
                        "updates": [
                            {
                                "member_id": str(self.suppressed.id),
                                "status": forbidden,
                            }
                        ]
                    }
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.suppressed.refresh_from_db()
        self.assertEqual(self.suppressed.status, CampaignMember.Status.SUPPRESSED)

    def test_suppressed_member_reported_as_completed_is_ignored(self):
        """Even a well-formed status cannot revive a suppressed member — it is terminal."""
        response = self._update(
            {"updates": [{"member_id": str(self.suppressed.id), "status": "COMPLETED"}]}
        )
        self.assertEqual(response.data["data"]["applied"], 0)
        self.suppressed.refresh_from_db()
        self.assertEqual(self.suppressed.status, CampaignMember.Status.SUPPRESSED)

    def test_batch_reports_partial_success(self):
        import uuid

        other = self._member("+14155552680")
        response = self._update(
            {
                "updates": [
                    {"member_id": str(self.pending.id), "status": "COMPLETED"},
                    {"member_id": str(uuid.uuid4()), "status": "COMPLETED"},
                    {"member_id": str(other.id), "status": "FAILED"},
                ]
            }
        )
        data = response.data["data"]
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["applied"], 2)
        results = {row["result"] for row in data["results"]}
        self.assertEqual(results, {"applied", "not_found"})

    def test_member_from_another_campaign_is_not_found(self):
        other_campaign = Campaign.objects.create(
            tenant=self.tenant, agent=self.agent, name="Other", status=Campaign.Status.RUNNING
        )
        contact = Contact.objects.create(
            tenant=self.tenant, phone_number="+14155552690"
        )
        stranger = CampaignMember.objects.create(
            tenant=self.tenant,
            campaign=other_campaign,
            contact=contact,
            phone_e164=contact.phone_e164,
        )

        response = self._update(
            {"updates": [{"member_id": str(stranger.id), "status": "COMPLETED"}]}
        )
        self.assertEqual(response.data["data"]["results"][0]["result"], "not_found")
        stranger.refresh_from_db()
        self.assertEqual(stranger.status, CampaignMember.Status.PENDING)

    def test_counters_are_recomputed(self):
        self._update(
            {"updates": [{"member_id": str(self.pending.id), "status": "COMPLETED"}]}
        )
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.completed_count, 1)
        self.assertEqual(self.campaign.pending_count, 0)
        self.assertEqual(self.campaign.suppressed_count, 1)

    def test_provider_call_id_links_the_call_log(self):
        log = AgentCallLog.objects.create(
            tenant=self.tenant,
            provider_call_id="prov-123",
            agent=self.agent,
            caller_number="+14155552671",
            direction=AgentCallLog.CallDirection.OUTBOUND,
        )
        self._update(
            {
                "updates": [
                    {
                        "member_id": str(self.pending.id),
                        "status": "COMPLETED",
                        "provider_call_id": "prov-123",
                    }
                ]
            }
        )
        log.refresh_from_db()
        self.assertEqual(log.campaign_member_id, self.pending.id)
        self.assertEqual(log.contact_id, self.pending.contact_id)

    def test_empty_batch_rejected(self):
        response = self._update({"updates": []})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_status_rejected(self):
        response = self._update(
            {"updates": [{"member_id": str(self.pending.id), "status": "NONSENSE"}]}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
