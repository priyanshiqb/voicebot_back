"""
Materialisation: freezing an audience into CampaignMember rows.

The invariants here are the ones that only break under retry or concurrency, so most of
these exercise a second run rather than a first.
"""

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from core.models.agent import Agent
from core.models.campaign import Campaign
from core.models.campaign_member import CampaignMember
from core.models.contact import Contact
from core.services import campaigns as campaign_service
from tenant.models.Tenant import Tenant


class MaterializeTestCase(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-mat")
        self.agent = Agent.objects.create(
            tenant=self.tenant, bot_name="Bot", persona_name="P"
        )
        self.campaign = Campaign.objects.create(
            tenant=self.tenant,
            agent=self.agent,
            name="Materialize",
            status=Campaign.Status.MATERIALIZING,
        )

    def _contact(self, phone, **kwargs):
        contact = Contact.objects.create(
            tenant=self.tenant, phone_number=phone, **kwargs
        )
        self.campaign.contacts.add(contact)
        return contact


class MaterializeTests(MaterializeTestCase):
    def test_builds_members_and_runs(self):
        self._contact("+14155552671")
        self._contact("+14155552672")

        campaign = campaign_service.materialize_campaign(self.campaign.pk)

        self.assertEqual(campaign.status, Campaign.Status.RUNNING)
        self.assertEqual(campaign.total_members, 2)
        self.assertEqual(campaign.pending_count, 2)
        self.assertIsNotNone(campaign.started_at)

    def test_sets_tenant_on_every_member(self):
        """bulk_create bypasses save() and perform_create — tenant must be explicit."""
        self._contact("+14155552671")
        campaign_service.materialize_campaign(self.campaign.pk)

        member = CampaignMember.objects.get(campaign=self.campaign)
        self.assertEqual(member.tenant_id, self.tenant.id)

    def test_freezes_the_phone_number(self):
        contact = self._contact("+14155552671")
        campaign_service.materialize_campaign(self.campaign.pk)

        contact.phone_number = "+14155552699"
        contact.save()

        member = CampaignMember.objects.get(campaign=self.campaign)
        self.assertEqual(member.phone_e164, "+14155552671")

    def test_suppressed_contacts_are_recorded_not_dropped(self):
        self._contact("+14155552671")
        self._contact("+14155552672", do_not_call=True)
        self._contact(
            "+14155552673", consent_status=Contact.ConsentStatus.WITHDRAWN
        )

        campaign = campaign_service.materialize_campaign(self.campaign.pk)

        self.assertEqual(campaign.total_members, 3)
        self.assertEqual(campaign.pending_count, 1)
        self.assertEqual(campaign.suppressed_count, 2)

        reasons = set(
            CampaignMember.objects.filter(
                campaign=self.campaign, status=CampaignMember.Status.SUPPRESSED
            ).values_list("suppression_reason", flat=True)
        )
        self.assertEqual(
            reasons,
            {
                CampaignMember.SuppressionReason.DO_NOT_CALL,
                CampaignMember.SuppressionReason.CONSENT_DENIED,
            },
        )

    def test_rerun_does_not_duplicate_members(self):
        """A retried task must be idempotent, not additive."""
        self._contact("+14155552671")
        campaign_service.materialize_campaign(self.campaign.pk)

        self.campaign.refresh_from_db()
        self.campaign.status = Campaign.Status.MATERIALIZING
        self.campaign.save(update_fields=["status"])

        campaign = campaign_service.materialize_campaign(self.campaign.pk)
        self.assertEqual(campaign.total_members, 1)
        self.assertEqual(CampaignMember.objects.count(), 1)

    def test_soft_deleted_contact_is_excluded(self):
        self._contact("+14155552671")
        gone = self._contact("+14155552672")
        gone.soft_delete()

        campaign = campaign_service.materialize_campaign(self.campaign.pk)
        self.assertEqual(campaign.total_members, 1)

    def test_all_suppressed_fails_with_a_reason(self):
        """
        Reachable despite the launch-time check: a contact can opt out in the gap
        between /launch/ and this task running.
        """
        self._contact("+14155552671", do_not_call=True)

        campaign = campaign_service.materialize_campaign(self.campaign.pk)
        self.assertEqual(campaign.status, Campaign.Status.FAILED)
        self.assertIn("suppressed", campaign.error_message)

    @override_settings(CAMPAIGN_MAX_MEMBERS=1)
    def test_over_the_member_cap_fails_without_partial_rows(self):
        self._contact("+14155552671")
        self._contact("+14155552672")

        campaign = campaign_service.materialize_campaign(self.campaign.pk)
        self.assertEqual(campaign.status, Campaign.Status.FAILED)
        self.assertEqual(CampaignMember.objects.count(), 0)

    @override_settings(CAMPAIGN_MATERIALIZE_BATCH_SIZE=2)
    def test_batching_covers_every_contact(self):
        for i in range(5):
            self._contact(f"+141555527{i}0")

        campaign = campaign_service.materialize_campaign(self.campaign.pk)
        self.assertEqual(campaign.total_members, 5)

    def test_wrong_status_is_a_no_op(self):
        """Guards against a duplicate task delivery re-running a finished campaign."""
        self._contact("+14155552671")
        self.campaign.status = Campaign.Status.RUNNING
        self.campaign.save(update_fields=["status"])

        campaign_service.materialize_campaign(self.campaign.pk)
        self.assertEqual(CampaignMember.objects.count(), 0)

    def test_missing_campaign_is_handled(self):
        import uuid

        self.assertIsNone(campaign_service.materialize_campaign(uuid.uuid4()))


class StartDueCampaignsTests(MaterializeTestCase):
    def setUp(self):
        super().setUp()
        self.campaign.status = Campaign.Status.SCHEDULED
        self.campaign.scheduled_start_at = timezone.now() - timezone.timedelta(
            minutes=1
        )
        self.campaign.schedule_timezone = "UTC"
        self.campaign.save()
        self._contact("+14155552671")

    def test_due_campaign_is_started(self):
        with self.captureOnCommitCallbacks(execute=True):
            started = campaign_service.start_due_campaigns()
        self.assertEqual(started, 1)
        self.campaign.refresh_from_db()
        # CELERY_TASK_ALWAYS_EAGER in test settings runs materialisation inline.
        self.assertEqual(self.campaign.status, Campaign.Status.RUNNING)

    def test_future_campaign_is_left_alone(self):
        self.campaign.scheduled_start_at = timezone.now() + timezone.timedelta(days=1)
        self.campaign.save(update_fields=["scheduled_start_at"])

        self.assertEqual(campaign_service.start_due_campaigns(), 0)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.SCHEDULED)

    def test_draft_campaign_is_not_swept(self):
        self.campaign.status = Campaign.Status.DRAFT
        self.campaign.save(update_fields=["status"])

        self.assertEqual(campaign_service.start_due_campaigns(), 0)

    def test_sweep_is_idempotent(self):
        with self.captureOnCommitCallbacks(execute=True):
            campaign_service.start_due_campaigns()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(campaign_service.start_due_campaigns(), 0)
        self.assertEqual(CampaignMember.objects.count(), 1)


class PreviewAudienceServiceTests(MaterializeTestCase):
    def test_counts_requested_matched_and_callable(self):
        good = self._contact("+14155552671")
        blocked = self._contact("+14155552672", do_not_call=True)
        import uuid

        result = campaign_service.preview_audience(
            self.tenant, [str(good.id), str(blocked.id), str(uuid.uuid4())]
        )
        self.assertEqual(result["requested"], 3)
        self.assertEqual(result["matched"], 2)
        self.assertEqual(result["callable"], 1)
        self.assertEqual(result["suppressed"], 1)

    def test_duplicate_ids_counted_once(self):
        good = self._contact("+14155552671")
        result = campaign_service.preview_audience(
            self.tenant, [str(good.id), str(good.id)]
        )
        self.assertEqual(result["requested"], 1)
        self.assertEqual(result["matched"], 1)
