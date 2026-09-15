"""Campaign CRUD, validation, tenant isolation, and lifecycle actions."""

from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.models.agent import Agent
from core.models.campaign import Campaign
from core.models.campaign_member import CampaignMember
from core.models.contact import Contact
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models import CustomUser as User

BASE = "/api/v1/core/campaigns/"
UTC = ZoneInfo("UTC")


class CampaignAPITestCase(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-camp-api")
        self.other_tenant = Tenant.objects.create(
            name="Globex", subdomain="globex-camp-api"
        )

        self.user = User.objects.create_user(
            username="alex", email="alex@example.com", password="password"
        )
        self.colleague = User.objects.create_user(
            username="casey", email="casey@example.com", password="password"
        )
        self.outsider = User.objects.create_user(
            username="bob", email="bob@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)
        TenantUserMapping.objects.create(user=self.colleague, tenant=self.tenant)
        TenantUserMapping.objects.create(user=self.outsider, tenant=self.other_tenant)

        self.client_alex = APIClient()
        self.client_alex.force_authenticate(user=self.user)
        self.client_casey = APIClient()
        self.client_casey.force_authenticate(user=self.colleague)
        self.client_bob = APIClient()
        self.client_bob.force_authenticate(user=self.outsider)

        self.agent = Agent.objects.create(
            tenant=self.tenant, bot_name="Sales Bot", persona_name="Persona"
        )
        self.other_agent = Agent.objects.create(
            tenant=self.other_tenant, bot_name="Other Bot", persona_name="Persona"
        )
        self.contact = Contact.objects.create(
            tenant=self.tenant, first_name="Ada", phone_number="+14155552671"
        )
        self.other_contact = Contact.objects.create(
            tenant=self.other_tenant, phone_number="+14155559999"
        )

    def _payload(self, **overrides):
        payload = {"name": "Q1 Leads", "agent": str(self.agent.id)}
        payload.update(overrides)
        return payload

    def _campaign(self, name="Existing", status_value=Campaign.Status.DRAFT, **kwargs):
        return Campaign.objects.create(
            tenant=self.tenant,
            agent=self.agent,
            name=name,
            status=status_value,
            **kwargs,
        )


class CampaignCreateTests(CampaignAPITestCase):
    def test_create_succeeds(self):
        response = self.client_alex.post(BASE, self._payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["data"]["status"], Campaign.Status.DRAFT)

    def test_create_assigns_tenant_and_audit(self):
        response = self.client_alex.post(BASE, self._payload(), format="json")
        campaign = Campaign.objects.get(pk=response.data["data"]["id"])
        self.assertEqual(campaign.tenant, self.tenant)
        self.assertEqual(campaign.created_by, self.user)

    def test_name_too_short(self):
        response = self.client_alex.post(
            BASE, self._payload(name="Q1"), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_name_too_long(self):
        response = self.client_alex.post(
            BASE, self._payload(name="x" * 101), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_name_rejects_punctuation(self):
        response = self.client_alex.post(
            BASE, self._payload(name="Q1 Leads!"), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_duplicate_name_rejected(self):
        self._campaign(name="Q1 Leads")
        response = self.client_alex.post(BASE, self._payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_duplicate_name_differing_by_case_rejected(self):
        self._campaign(name="Q1 Leads")
        response = self.client_alex.post(
            BASE, self._payload(name="q1 LEADS"), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_same_name_allowed_in_another_tenant(self):
        self._campaign(name="Q1 Leads")
        response = self.client_bob.post(
            BASE,
            {"name": "Q1 Leads", "agent": str(self.other_agent.id)},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_description_too_short(self):
        response = self.client_alex.post(
            BASE, self._payload(description="ab"), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_description_too_long(self):
        response = self.client_alex.post(
            BASE, self._payload(description="x" * 501), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_blank_description_allowed(self):
        response = self.client_alex.post(
            BASE, self._payload(description=""), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_another_tenants_agent_rejected(self):
        """Cross-tenant write guard — the highest-value test in this file."""
        response = self.client_alex.post(
            BASE, self._payload(agent=str(self.other_agent.id)), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Campaign.objects.filter(name="Q1 Leads").exists())

    def test_another_tenants_contact_rejected(self):
        response = self.client_alex.post(
            BASE,
            self._payload(contacts=[str(self.other_contact.id)]),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Campaign.objects.filter(name="Q1 Leads").exists())

    def test_inactive_agent_rejected(self):
        self.agent.deactivate()
        response = self.client_alex.post(BASE, self._payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_no_active_bot_returns_machine_readable_code(self):
        self.agent.soft_delete()
        response = self.client_alex.post(BASE, self._payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["errors"]["code"], "NO_ACTIVE_BOT")

    def test_filter_audience_source_not_yet_supported(self):
        response = self.client_alex.post(
            BASE, self._payload(audience_source="FILTER"), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_settings(CAMPAIGN_MAX_SELECTED_CONTACTS=2)
    def test_contact_cap_enforced(self):
        contacts = [self.contact]
        for i in range(2):
            contacts.append(
                Contact.objects.create(
                    tenant=self.tenant, phone_number=f"+1415555300{i}"
                )
            )
        response = self.client_alex.post(
            BASE,
            self._payload(contacts=[str(c.id) for c in contacts]),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_status_is_not_client_writable(self):
        response = self.client_alex.post(
            BASE, self._payload(status=Campaign.Status.RUNNING), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["data"]["status"], Campaign.Status.DRAFT)


class CampaignScheduleAPITests(CampaignAPITestCase):
    def test_day_first_date_format(self):
        """
        01/02/2027 must be 1 February, not 2 January. If input_formats is ordered
        month-first this passes silently and every campaign fires ten months late.
        """
        response = self.client_alex.post(
            BASE,
            self._payload(start_date="01/02/2027", start_time="09:00"),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["data"]["start_local"]["date"], "2027-02-01")

    def test_iso_date_also_accepted(self):
        response = self.client_alex.post(
            BASE,
            self._payload(start_date="2027-02-01", start_time="09:00"),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_twelve_hour_time_accepted(self):
        response = self.client_alex.post(
            BASE,
            self._payload(start_date="01/02/2027", start_time="09:30 AM"),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["data"]["start_local"]["time"], "09:30")

    def test_past_start_rejected(self):
        response = self.client_alex.post(
            BASE,
            self._payload(start_date="01/02/2020", start_time="09:00"),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_date_without_time_rejected(self):
        response = self.client_alex.post(
            BASE, self._payload(start_date="01/02/2027"), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_time_without_date_rejected(self):
        response = self.client_alex.post(
            BASE, self._payload(start_time="09:00"), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_no_schedule_is_allowed(self):
        response = self.client_alex.post(BASE, self._payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(response.data["data"]["start_local"])


class CampaignEditGuardTests(CampaignAPITestCase):
    def test_draft_allows_full_edit(self):
        campaign = self._campaign()
        response = self.client_alex.patch(
            f"{BASE}{campaign.id}/",
            {"agent": str(self.agent.id), "name": "Renamed"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_running_blocks_agent_change(self):
        campaign = self._campaign(status_value=Campaign.Status.RUNNING)
        response = self.client_alex.patch(
            f"{BASE}{campaign.id}/", {"agent": str(self.agent.id)}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_running_blocks_audience_change(self):
        campaign = self._campaign(status_value=Campaign.Status.RUNNING)
        response = self.client_alex.patch(
            f"{BASE}{campaign.id}/",
            {"contacts": [str(self.contact.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_running_allows_rename(self):
        campaign = self._campaign(status_value=Campaign.Status.RUNNING)
        response = self.client_alex.patch(
            f"{BASE}{campaign.id}/", {"name": "Renamed"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_terminal_blocks_everything(self):
        campaign = self._campaign(status_value=Campaign.Status.COMPLETED)
        response = self.client_alex.patch(
            f"{BASE}{campaign.id}/", {"name": "Renamed"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)


class CampaignDeleteTests(CampaignAPITestCase):
    def test_delete_draft(self):
        campaign = self._campaign()
        response = self.client_alex.delete(f"{BASE}{campaign.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_delete_running_refused(self):
        campaign = self._campaign(status_value=Campaign.Status.RUNNING)
        response = self.client_alex.delete(f"{BASE}{campaign.id}/")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        campaign.refresh_from_db()
        self.assertFalse(campaign.is_deleted)

    def test_bulk_delete_cannot_bypass_the_guard(self):
        """
        BaseViewSet.bulk_delete calls soft_delete() directly, skipping destroy(). The
        guard lives on the model precisely so this route is closed too.
        """
        campaign = self._campaign(status_value=Campaign.Status.RUNNING)
        response = self.client_alex.post(
            f"{BASE}bulk_delete/", {"ids": [str(campaign.id)]}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        campaign.refresh_from_db()
        self.assertFalse(campaign.is_deleted)


class CampaignTenantIsolationTests(CampaignAPITestCase):
    def test_other_tenant_cannot_retrieve(self):
        campaign = self._campaign()
        response = self.client_bob.get(f"{BASE}{campaign.id}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_other_tenant_cannot_launch(self):
        campaign = self._campaign()
        response = self.client_bob.post(f"{BASE}{campaign.id}/launch/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_other_tenant_campaigns_absent_from_list(self):
        self._campaign(name="Mine")
        Campaign.objects.create(
            tenant=self.other_tenant, agent=self.other_agent, name="Theirs"
        )
        response = self.client_alex.get(BASE)
        names = [row["name"] for row in response.data["data"]]
        self.assertIn("Mine", names)
        self.assertNotIn("Theirs", names)

    def test_colleague_in_same_tenant_can_act(self):
        """Campaigns are tenant-wide; attribution records who actually acted."""
        campaign = self._campaign()
        campaign.contacts.add(self.contact)
        with mock.patch("core.views.campaign.materialize_campaign.delay"):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client_casey.post(f"{BASE}{campaign.id}/launch/")
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        campaign.refresh_from_db()
        self.assertEqual(campaign.launched_by, self.colleague)


class CampaignLaunchTests(CampaignAPITestCase):
    def setUp(self):
        super().setUp()
        self.campaign = self._campaign(name="Launchable")
        self.campaign.contacts.add(self.contact)

    def test_launch_without_schedule_materializes(self):
        with mock.patch(
            "core.views.campaign.materialize_campaign.delay"
        ) as delayed:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client_alex.post(f"{BASE}{self.campaign.id}/launch/")
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.MATERIALIZING)
        self.assertIsNotNone(self.campaign.handoff_id)
        delayed.assert_called_once()

    def test_launch_with_future_schedule_waits_for_the_sweep(self):
        future = timezone.now() + timedelta(days=2)
        self.campaign.scheduled_start_at = future
        self.campaign.schedule_timezone = "UTC"
        self.campaign.save()

        with mock.patch(
            "core.views.campaign.materialize_campaign.delay"
        ) as delayed:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client_alex.post(f"{BASE}{self.campaign.id}/launch/")
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.SCHEDULED)
        delayed.assert_not_called()

    def test_second_launch_is_refused(self):
        with mock.patch("core.views.campaign.materialize_campaign.delay"):
            self.client_alex.post(f"{BASE}{self.campaign.id}/launch/")
            response = self.client_alex.post(f"{BASE}{self.campaign.id}/launch/")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_launch_with_empty_audience_refused(self):
        empty = self._campaign(name="Empty")
        response = self.client_alex.post(f"{BASE}{empty.id}/launch/")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        empty.refresh_from_db()
        self.assertEqual(empty.status, Campaign.Status.DRAFT)

    def test_launch_with_fully_suppressed_audience_refused(self):
        """Refused up front rather than accepted and quietly landing in FAILED."""
        self.contact.do_not_call = True
        self.contact.save()

        response = self.client_alex.post(f"{BASE}{self.campaign.id}/launch/")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.DRAFT)

    def test_launch_with_deleted_agent_refused(self):
        Agent.all_objects.filter(pk=self.agent.pk).update(is_deleted=True)
        response = self.client_alex.post(f"{BASE}{self.campaign.id}/launch/")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)


class CampaignTransitionTests(CampaignAPITestCase):
    def test_pause_and_resume(self):
        campaign = self._campaign(status_value=Campaign.Status.RUNNING)

        response = self.client_alex.post(f"{BASE}{campaign.id}/pause/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.PAUSED)
        self.assertEqual(campaign.paused_by, self.user)

        response = self.client_alex.post(f"{BASE}{campaign.id}/resume/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.RUNNING)

    def test_pause_from_draft_refused(self):
        campaign = self._campaign()
        response = self.client_alex.post(f"{BASE}{campaign.id}/pause/")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_resume_from_running_refused(self):
        campaign = self._campaign(status_value=Campaign.Status.RUNNING)
        response = self.client_alex.post(f"{BASE}{campaign.id}/resume/")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_cancel_flips_pending_members_only(self):
        campaign = self._campaign(status_value=Campaign.Status.RUNNING)
        pending = CampaignMember.objects.create(
            tenant=self.tenant,
            campaign=campaign,
            contact=self.contact,
            phone_e164=self.contact.phone_e164,
        )
        blocked_contact = Contact.objects.create(
            tenant=self.tenant, phone_number="+14155552680", do_not_call=True
        )
        suppressed = CampaignMember.objects.create(
            tenant=self.tenant,
            campaign=campaign,
            contact=blocked_contact,
            phone_e164=blocked_contact.phone_e164,
            status=CampaignMember.Status.SUPPRESSED,
            suppression_reason=CampaignMember.SuppressionReason.DO_NOT_CALL,
        )

        response = self.client_alex.post(f"{BASE}{campaign.id}/cancel/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        pending.refresh_from_db()
        suppressed.refresh_from_db()
        self.assertEqual(pending.status, CampaignMember.Status.CANCELLED)
        # The suppression reason is a compliance record, not a work item.
        self.assertEqual(suppressed.status, CampaignMember.Status.SUPPRESSED)

        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.CANCELLED)
        self.assertEqual(campaign.cancelled_by, self.user)

    def test_cancel_terminal_campaign_refused(self):
        campaign = self._campaign(status_value=Campaign.Status.COMPLETED)
        response = self.client_alex.post(f"{BASE}{campaign.id}/cancel/")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)


class CampaignAudienceViewTests(CampaignAPITestCase):
    def setUp(self):
        super().setUp()
        self.campaign = self._campaign(name="Audience")
        self.blocked = Contact.objects.create(
            tenant=self.tenant, phone_number="+14155552690", do_not_call=True
        )
        CampaignMember.objects.create(
            tenant=self.tenant,
            campaign=self.campaign,
            contact=self.contact,
            phone_e164=self.contact.phone_e164,
        )
        CampaignMember.objects.create(
            tenant=self.tenant,
            campaign=self.campaign,
            contact=self.blocked,
            phone_e164=self.blocked.phone_e164,
            status=CampaignMember.Status.SUPPRESSED,
            suppression_reason=CampaignMember.SuppressionReason.DO_NOT_CALL,
        )

    def test_members_list(self):
        response = self.client_alex.get(f"{BASE}{self.campaign.id}/members/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 2)

    def test_members_expose_name_and_phone_but_not_full_contact(self):
        response = self.client_alex.get(f"{BASE}{self.campaign.id}/members/")
        row = response.data["data"][0]
        self.assertIn("full_name", row)
        self.assertIn("phone_e164", row)
        self.assertNotIn("email_address", row)
        self.assertNotIn("notes", row)

    def test_members_filter_by_status(self):
        response = self.client_alex.get(
            f"{BASE}{self.campaign.id}/members/?status=SUPPRESSED"
        )
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(
            response.data["data"][0]["suppression_reason"], "DO_NOT_CALL"
        )

    def test_members_filter_by_comma_separated_statuses(self):
        response = self.client_alex.get(
            f"{BASE}{self.campaign.id}/members/?status=PENDING, SUPPRESSED"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            {row["status"] for row in response.data["data"]}, {"PENDING", "SUPPRESSED"}
        )

    def test_members_filter_by_repeated_status(self):
        """The key repeated must not collapse to its last value."""
        response = self.client_alex.get(
            f"{BASE}{self.campaign.id}/members/?status=PENDING&status=SUPPRESSED"
        )
        self.assertEqual(len(response.data["data"]), 2)

    def test_several_statuses_still_exclude_the_rest(self):
        response = self.client_alex.get(
            f"{BASE}{self.campaign.id}/members/?status=COMPLETED,SUPPRESSED"
        )
        self.assertEqual(
            [row["status"] for row in response.data["data"]], ["SUPPRESSED"]
        )

    def test_stats_breaks_down_suppression(self):
        response = self.client_alex.get(f"{BASE}{self.campaign.id}/stats/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["data"]["suppressed_by_reason"]["DO_NOT_CALL"], 1
        )

    def test_other_tenant_cannot_read_members(self):
        response = self.client_bob.get(f"{BASE}{self.campaign.id}/members/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class PreviewAudienceTests(CampaignAPITestCase):
    def test_preview_separates_callable_from_suppressed(self):
        blocked = Contact.objects.create(
            tenant=self.tenant, phone_number="+14155552691", do_not_call=True
        )
        response = self.client_alex.post(
            f"{BASE}preview-audience/",
            {"contact_ids": [str(self.contact.id), str(blocked.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertEqual(data["matched"], 2)
        self.assertEqual(data["callable"], 1)
        self.assertEqual(data["suppressed_by_reason"]["DO_NOT_CALL"], 1)

    def test_preview_ignores_other_tenants_contacts(self):
        response = self.client_alex.post(
            f"{BASE}preview-audience/",
            {"contact_ids": [str(self.other_contact.id)]},
            format="json",
        )
        data = response.data["data"]
        self.assertEqual(data["requested"], 1)
        self.assertEqual(data["matched"], 0)

    def test_preview_works_before_any_campaign_exists(self):
        self.assertEqual(Campaign.objects.count(), 0)
        response = self.client_alex.post(
            f"{BASE}preview-audience/",
            {"contact_ids": [str(self.contact.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class CampaignSchemaTests(CampaignAPITestCase):
    """
    Regression: the campaign viewset must appear in the OpenAPI document.

    Serializers are instantiated during schema generation with no real request. An
    exception raised while resolving the caller's tenant silently drops the whole
    viewset from the schema — a failure that no functional test would notice.
    """

    def test_viewset_is_present_in_the_generated_schema(self):
        from drf_spectacular.generators import SchemaGenerator

        schema = SchemaGenerator().get_schema(request=None, public=True)
        self.assertIn("/api/v1/core/campaigns/", schema["paths"])
        self.assertIn(
            "/api/v1/core/campaigns/preview-audience/", schema["paths"]
        )

    def test_serializer_without_a_request_offers_no_choices(self):
        """Falling back to an unfiltered queryset here would be the cross-tenant hole."""
        from core.serializers.campaign import CampaignSerializer

        serializer = CampaignSerializer(context={})
        self.assertEqual(serializer.fields["agent"].queryset.count(), 0)
        self.assertEqual(
            serializer.fields["contacts"].child_relation.queryset.count(), 0
        )
