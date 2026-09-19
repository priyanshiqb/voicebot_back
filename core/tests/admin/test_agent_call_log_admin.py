"""The call log admin: nothing missing from the page, and any id finds the call."""

import uuid

from django.contrib.admin.sites import site
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.plugins.otp_totp.models import TOTPDevice

from core.admin.agent_call_log_admin import JSON_DISPLAY_FIELDS
from core.models.agent import Agent
from core.models.agent_call_log import AgentCallLog
from core.models.agent_call_recording import AgentCallRecording
from core.models.campaign import Campaign
from core.models.campaign_member import CampaignMember
from core.models.contact import Contact
from tenant.models.Tenant import Tenant
from user.models import CustomUser as User


class CallLogAdminTestCase(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-calllog-admin")
        self.admin = User.objects.create_superuser(
            username="root", email="root@example.com", password="password"
        )
        self.client.force_login(self.admin)
        # The admin site is wrapped in `AdminSiteOTPRequired` (config/urls.py), so a
        # logged-in superuser is still redirected until the session names a verified
        # device. Without this every page assertion below would read as a 302.
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
            status=Campaign.Status.RUNNING,
        )
        self.member = CampaignMember.objects.create(
            tenant=self.tenant, campaign=self.campaign, contact=self.contact,
            phone_e164=self.contact.phone_e164, status=CampaignMember.Status.COMPLETED,
        )
        self.recording = AgentCallRecording.objects.create(
            tenant=self.tenant, file="call-recordings/ada.mp3",
            original_name="ada.mp3", file_size_in_bytes=2411008,
        )
        self.log = AgentCallLog.objects.create(
            tenant=self.tenant, agent=self.agent, provider_call_id="call-admin-1",
            caller_number="+18005550100", callee_number=self.contact.phone_e164,
            caller_name="Sales Bot", contact=self.contact, campaign_member=self.member,
            direction=AgentCallLog.CallDirection.OUTBOUND, campaign_id="rabi-2026",
            duration_seconds=95, credits_charged=10, outcome="information provided",
            mood="Positive", mood_confidence=0.87, preferred_language="Hindi",
            ai_summary="Asked for a callback next week.",
            follow_up_detail="Call again after Diwali.",
            transcript=[{"id": "t-0", "speaker": "bot", "text": "Good morning."}],
            probing_questions=[
                {
                    "question": "how do you manage your leads?",
                    "isPositive": True,
                    "answerSummary": "A spreadsheet today.",
                }
            ],
            probing_questions_count=1,
            recording_file=self.recording,
            tags=["vip"],
        )

        self.model_admin = site._registry[AgentCallLog]
        self.request = RequestFactory().get("/admin/core/agentcalllog/")
        self.request.user = self.admin

    def search(self, term):
        results, _ = self.model_admin.get_search_results(
            self.request, AgentCallLog.objects.all(), term
        )
        return list(results)


class PageCompletenessTests(CallLogAdminTestCase):
    def test_every_field_on_the_model_has_a_home_on_the_page(self):
        """The point of this page is that nothing is missing. A field added to the model
        and forgotten here would be invisible to support, with no error anywhere."""
        shown = set()
        for _title, options in self.model_admin.fieldsets:
            for entry in options["fields"]:
                shown.update([entry] if isinstance(entry, str) else entry)

        missing = []
        for field in AgentCallLog._meta.get_fields():
            if not getattr(field, "concrete", False):
                continue
            if field.name in shown or JSON_DISPLAY_FIELDS.get(field.name) in shown:
                continue
            missing.append(field.name)

        self.assertEqual(missing, [], f"not shown on the admin page: {missing}")

    def test_the_change_page_renders_the_whole_record(self):
        url = reverse("admin:core_agentcalllog_change", args=[self.log.pk])

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        for expected in [
            "call-admin-1",                      # provider call id
            "rabi-2026",                         # free-text campaign tag
            "Good morning.",                     # transcript, rendered
            "how do you manage your leads?",     # probing payload, rendered
            "ada.mp3",                           # recording
            "Asked for a callback next week.",   # summary
            "Rabi Season",                       # campaign link
        ]:
            self.assertIn(expected, page, expected)

    def test_the_changelist_renders(self):
        response = self.client.get(reverse("admin:core_agentcalllog_changelist"))

        self.assertEqual(response.status_code, 200)

    def test_a_call_with_nothing_filled_in_still_renders(self):
        """Most fields are nullable and the runner sends what it has. A page that only
        works for a complete record is a page that breaks when it is needed."""
        bare = AgentCallLog.objects.create(
            tenant=self.tenant, agent=self.agent, provider_call_id="call-admin-bare",
            caller_number="+18005550100",
            direction=AgentCallLog.CallDirection.INBOUND,
        )

        response = self.client.get(
            reverse("admin:core_agentcalllog_change", args=[bare.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Not linked to any campaign member", response.content.decode())

    def test_call_logs_cannot_be_added_by_hand(self):
        self.assertFalse(self.model_admin.has_add_permission(self.request))


class SearchTests(CallLogAdminTestCase):
    def test_the_call_logs_own_id_finds_it(self):
        self.assertEqual(self.search(str(self.log.pk)), [self.log])

    def test_a_campaign_member_id_finds_that_members_calls(self):
        """The id a campaign report puts in front of people is the **member** id."""
        self.assertEqual(self.search(str(self.member.pk)), [self.log])

    def test_a_contact_id_finds_their_calls(self):
        self.assertEqual(self.search(str(self.contact.pk)), [self.log])

    def test_an_agent_id_finds_its_calls(self):
        self.assertEqual(self.search(str(self.agent.pk)), [self.log])

    def test_a_campaign_id_finds_the_calls_made_for_it(self):
        self.assertEqual(self.search(str(self.campaign.pk)), [self.log])

    def test_a_recording_id_finds_the_call_it_belongs_to(self):
        self.assertEqual(self.search(str(self.recording.pk)), [self.log])

    def test_an_unknown_uuid_finds_nothing_rather_than_everything(self):
        self.assertEqual(self.search(str(uuid.uuid4())), [])

    def test_text_search_still_works(self):
        for term in ["call-admin-1", "9897", "Lovelace", "information provided", "Rabi"]:
            with self.subTest(term=term):
                found = self.search(term if term != "9897" else "5552671")
                self.assertEqual(found, [self.log], term)

    def test_a_search_that_is_neither_uuid_nor_match_is_quietly_empty(self):
        """`abc-def` looks enough like an id to be pasted, and must not raise."""
        self.assertEqual(self.search("abc-def"), [])

    def test_searching_by_phone_finds_the_call_from_either_leg(self):
        self.assertEqual(self.search("+18005550100"), [self.log])
        self.assertEqual(self.search(self.contact.phone_e164), [self.log])
