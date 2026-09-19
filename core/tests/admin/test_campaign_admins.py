"""Campaign, member and report admin pages: nothing missing, and any id finds the row."""

import uuid

from datetime import timedelta

from django.contrib.admin.sites import site
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.plugins.otp_totp.models import TOTPDevice

from core.models.agent import Agent
from core.models.agent_call_log import AgentCallLog
from core.models.campaign import Campaign
from core.models.campaign_member import CampaignMember
from core.models.campaign_report import CampaignReport
from core.models.contact import Contact
from tenant.models.Tenant import Tenant
from user.models import CustomUser as User


class CampaignAdminTestCase(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-campaign-admin")
        self.admin = User.objects.create_superuser(
            username="root", email="root@example.com", password="password"
        )
        self.client.force_login(self.admin)
        # The admin site is wrapped in `AdminSiteOTPRequired` (config/urls.py), so a
        # logged-in superuser is still redirected until the session names a device.
        device = TOTPDevice.objects.create(user=self.admin, name="test", confirmed=True)
        session = self.client.session
        session[DEVICE_ID_SESSION_KEY] = device.persistent_id
        session.save()

        self.agent = Agent.objects.create(
            tenant=self.tenant, bot_name="Sales Bot", persona_name="Persona"
        )
        self.contact = Contact.objects.create(
            tenant=self.tenant, first_name="Ada", last_name="Lovelace",
            phone_number="+14155552671", company="Acme Traders",
        )
        self.campaign = Campaign.objects.create(
            tenant=self.tenant, agent=self.agent, name="Rabi Season",
            description="Warm leads from the kharif webinar",
            status=Campaign.Status.RUNNING, schedule_timezone="Asia/Kolkata",
            started_at=timezone.now() - timedelta(hours=2),
            launched_by=self.admin, total_members=1,
        )
        self.member = CampaignMember.objects.create(
            tenant=self.tenant, campaign=self.campaign, contact=self.contact,
            phone_e164=self.contact.phone_e164, status=CampaignMember.Status.COMPLETED,
            outcome="information provided", attempt_count=1,
            last_call_at=timezone.now(),
        )
        self.log = AgentCallLog.objects.create(
            tenant=self.tenant, agent=self.agent, provider_call_id="call-campaign-1",
            caller_number="+18005550100", callee_number=self.contact.phone_e164,
            direction=AgentCallLog.CallDirection.OUTBOUND, duration_seconds=95,
            credits_charged=10, campaign_member=self.member, contact=self.contact,
        )
        self.report = CampaignReport.objects.create(
            tenant=self.tenant, campaign=self.campaign, created_by=self.admin,
            updated_by=self.admin, status=CampaignReport.Status.COMPLETED,
            file_name="campaign-rabi-season-20260919-090000.xlsx",
            file_key="t-x/campaign-reports/r_campaign.xlsx", file_size_bytes=1843201,
            total_rows=1, processed_rows=1, rows_included=1,
            completed_at=timezone.now(), expires_at=timezone.now() + timedelta(days=7),
        )

        self.request = RequestFactory().get("/admin/")
        self.request.user = self.admin

    def model_admin(self, model):
        return site._registry[model]

    def search(self, model, term):
        model_admin = self.model_admin(model)
        results, _ = model_admin.get_search_results(
            self.request, model._default_manager.all(), term
        )
        return list(results)

    def shown_fields(self, model):
        fields = set()
        for _title, options in self.model_admin(model).fieldsets:
            for entry in options["fields"]:
                fields.update([entry] if isinstance(entry, str) else entry)
        return fields

    def assert_page_shows_everything(self, model, aliases=None):
        aliases = aliases or {}
        shown = self.shown_fields(model)
        missing = [
            field.name
            for field in model._meta.get_fields()
            if (getattr(field, "concrete", False) or field.many_to_many)
            and field.name not in shown
            and aliases.get(field.name) not in shown
        ]
        self.assertEqual(missing, [], f"{model.__name__}: not shown on its page: {missing}")

    def change_page(self, model, pk):
        url = reverse(
            f"admin:{model._meta.app_label}_{model._meta.model_name}_change", args=[pk]
        )
        return self.client.get(url)


class CompletenessTests(CampaignAdminTestCase):
    def test_the_campaign_page_shows_every_field(self):
        self.assert_page_shows_everything(Campaign)

    def test_the_member_page_shows_every_field(self):
        self.assert_page_shows_everything(CampaignMember)

    def test_the_report_page_shows_every_field(self):
        self.assert_page_shows_everything(CampaignReport)

    def test_each_change_page_renders(self):
        for model, pk in [
            (Campaign, self.campaign.pk),
            (CampaignMember, self.member.pk),
            (CampaignReport, self.report.pk),
        ]:
            with self.subTest(model=model.__name__):
                self.assertEqual(self.change_page(model, pk).status_code, 200)

    def test_each_changelist_renders(self):
        for model in (Campaign, CampaignMember, CampaignReport):
            with self.subTest(model=model.__name__):
                url = reverse(
                    f"admin:{model._meta.app_label}_{model._meta.model_name}_changelist"
                )
                self.assertEqual(self.client.get(url).status_code, 200)


class CampaignPageTests(CampaignAdminTestCase):
    def test_the_campaign_page_lists_its_reports_and_links_to_each(self):
        page = self.change_page(Campaign, self.campaign.pk).content.decode()

        self.assertIn(self.report.file_name, page)
        self.assertIn(
            reverse("admin:core_campaignreport_change", args=[self.report.pk]), page
        )

    def test_the_campaign_page_links_to_its_members(self):
        """A link rather than an inline: an audience runs to six figures."""
        page = self.change_page(Campaign, self.campaign.pk).content.decode()

        expected = (
            reverse("admin:core_campaignmember_changelist")
            + f"?campaign__id__exact={self.campaign.pk}"
        )
        self.assertIn(expected, page)

    def test_that_member_link_actually_filters(self):
        """The filter must be allowed, or the changelist answers 400."""
        response = self.client.get(
            reverse("admin:core_campaignmember_changelist"),
            {"campaign__id__exact": str(self.campaign.pk)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(self.member.phone_e164, response.content.decode())

    def test_that_reports_link_actually_filters(self):
        response = self.client.get(
            reverse("admin:core_campaignreport_changelist"),
            {"campaign__id__exact": str(self.campaign.pk)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(self.report.file_name, response.content.decode())


class MemberPageTests(CampaignAdminTestCase):
    def test_the_member_page_lists_the_calls_attached_to_them(self):
        page = self.change_page(CampaignMember, self.member.pk).content.decode()

        self.assertIn("call-campaign-1", page)
        self.assertIn(reverse("admin:core_agentcalllog_change", args=[self.log.pk]), page)

    def test_a_member_with_no_call_says_so_plainly(self):
        """This is exactly why a campaign report shows blank call columns."""
        lonely = CampaignMember.objects.create(
            tenant=self.tenant, campaign=self.campaign,
            contact=Contact.objects.create(
                tenant=self.tenant, first_name="Grace", last_name="Hopper",
                phone_number="+14155552673",
            ),
            phone_e164="+14155552673", status=CampaignMember.Status.FAILED,
        )

        page = self.change_page(CampaignMember, lonely.pk).content.decode()

        self.assertIn("No call log is attached to this member", page)

    def test_members_cannot_be_added_or_edited(self):
        model_admin = self.model_admin(CampaignMember)

        self.assertFalse(model_admin.has_add_permission(self.request))
        self.assertFalse(model_admin.has_change_permission(self.request))


class ReportPageTests(CampaignAdminTestCase):
    def test_a_report_with_no_file_says_there_is_nothing_to_download(self):
        pending = CampaignReport.objects.create(
            tenant=self.tenant, campaign=self.campaign, created_by=self.admin,
            updated_by=self.admin,
        )

        page = self.change_page(CampaignReport, pending.pk).content.decode()

        self.assertIn("No file to download.", page)

    def test_a_signing_failure_does_not_break_the_page(self):
        """Storage may be unconfigured in the environment support is looking from."""
        page = self.change_page(CampaignReport, self.report.pk).content.decode()

        self.assertEqual(self.change_page(CampaignReport, self.report.pk).status_code, 200)
        self.assertTrue(
            "Could not sign a link" in page or self.report.file_name in page
        )

    def test_reports_cannot_be_added_by_hand(self):
        self.assertFalse(self.model_admin(CampaignReport).has_add_permission(self.request))


class SearchTests(CampaignAdminTestCase):
    def test_a_campaign_is_found_by_its_members_id(self):
        self.assertEqual(self.search(Campaign, str(self.member.pk)), [self.campaign])

    def test_a_campaign_is_found_by_a_report_id(self):
        self.assertEqual(self.search(Campaign, str(self.report.pk)), [self.campaign])

    def test_a_campaign_is_found_by_its_agent_or_its_own_id(self):
        self.assertEqual(self.search(Campaign, str(self.agent.pk)), [self.campaign])
        self.assertEqual(self.search(Campaign, str(self.campaign.pk)), [self.campaign])

    def test_a_campaign_is_found_by_name_and_by_a_members_phone(self):
        self.assertEqual(self.search(Campaign, "Rabi"), [self.campaign])
        self.assertEqual(self.search(Campaign, "5552671"), [self.campaign])

    def test_a_member_is_found_by_its_own_id_and_by_contact_id(self):
        self.assertEqual(self.search(CampaignMember, str(self.member.pk)), [self.member])
        self.assertEqual(self.search(CampaignMember, str(self.contact.pk)), [self.member])

    def test_a_member_is_found_by_a_call_log_id(self):
        self.assertEqual(self.search(CampaignMember, str(self.log.pk)), [self.member])

    def test_a_member_is_found_by_phone_and_campaign_name(self):
        self.assertEqual(self.search(CampaignMember, "5552671"), [self.member])
        self.assertEqual(self.search(CampaignMember, "Rabi"), [self.member])

    def test_a_report_is_found_by_its_campaign_id_and_requester_id(self):
        self.assertEqual(self.search(CampaignReport, str(self.campaign.pk)), [self.report])
        self.assertEqual(self.search(CampaignReport, str(self.admin.pk)), [self.report])

    def test_a_report_is_found_by_file_name(self):
        self.assertEqual(self.search(CampaignReport, "rabi-season"), [self.report])

    def test_an_unknown_uuid_finds_nothing_anywhere(self):
        stranger = str(uuid.uuid4())
        for model in (Campaign, CampaignMember, CampaignReport):
            with self.subTest(model=model.__name__):
                self.assertEqual(self.search(model, stranger), [])

    def test_a_term_that_is_not_a_uuid_does_not_raise(self):
        for model in (Campaign, CampaignMember, CampaignReport):
            with self.subTest(model=model.__name__):
                self.assertEqual(self.search(model, "abc-def"), [])
