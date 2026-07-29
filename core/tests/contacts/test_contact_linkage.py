"""
Linking call logs and booking participants to Contacts.

Both models store a bare phone string, so resolution goes through E.164 normalization
rather than string equality — otherwise "+1 (415) 555-2671" would never match
"+14155552671".
"""

from django.test import TestCase, override_settings

from core.models.agent_call_log import AgentCallLog
from core.models.agent import Agent
from core.models.agent_template import AgentTemplate
from core.models.contact import Contact
from core.services import contact_resolution
from tenant.models.Tenant import Tenant


@override_settings(CONTACT_DEFAULT_PHONE_REGION="US")
class CallLogLinkageTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-link")
        self.other_tenant = Tenant.objects.create(name="Globex", subdomain="globex-link")
        self.template = AgentTemplate.objects.create(
            tenant=self.tenant, name="Template Link"
        )
        self.agent = Agent.objects.create(
            tenant=self.tenant, bot_name="Bot Link", agent_template=self.template
        )
        self.contact = Contact.objects.create(
            tenant=self.tenant, first_name="Ada", phone_number="+14155557001"
        )

    def _make_call(self, caller_number, call_id="call-1"):
        return AgentCallLog.objects.create(
            tenant=self.tenant,
            agent=self.agent,
            provider_call_id=call_id,
            caller_number=caller_number,
            direction=AgentCallLog.CallDirection.INBOUND,
        )

    def test_call_log_links_on_create(self):
        call = self._make_call("+14155557001")
        self.assertEqual(call.contact_id, self.contact.id)

    def test_link_survives_different_formatting(self):
        """The whole point of normalizing: formatting must not defeat the match."""
        for index, spelling in enumerate(
            ["+1 (415) 555-7001", "+1-415-555-7001", "(415) 555-7001"]
        ):
            with self.subTest(spelling=spelling):
                call = self._make_call(spelling, call_id=f"call-fmt-{index}")
                self.assertEqual(call.contact_id, self.contact.id)

    def test_unknown_number_leaves_contact_null(self):
        call = self._make_call("+14155559999", call_id="call-unknown")
        self.assertIsNone(call.contact_id)

    def test_unparseable_number_does_not_break_the_call_log(self):
        """Resolution is a convenience, never a gate on recording the call."""
        call = self._make_call("not-a-number", call_id="call-bad")
        self.assertIsNone(call.contact_id)
        self.assertTrue(AgentCallLog.objects.filter(pk=call.pk).exists())

    def test_does_not_link_across_tenants(self):
        Contact.objects.create(
            tenant=self.other_tenant, phone_number="+14155557500"
        )
        call = self._make_call("+14155557500", call_id="call-cross")
        self.assertIsNone(call.contact_id)

    def test_deleting_a_contact_keeps_call_history(self):
        call = self._make_call("+14155557001", call_id="call-keep")
        self.contact.delete()

        call.refresh_from_db()
        self.assertIsNone(call.contact_id)
        self.assertTrue(AgentCallLog.objects.filter(pk=call.pk).exists())

    def test_explicit_contact_is_not_overwritten(self):
        other = Contact.objects.create(
            tenant=self.tenant, phone_number="+14155557800"
        )
        call = AgentCallLog.objects.create(
            tenant=self.tenant,
            agent=self.agent,
            provider_call_id="call-explicit",
            caller_number="+14155557001",
            direction=AgentCallLog.CallDirection.INBOUND,
            contact=other,
        )
        self.assertEqual(call.contact_id, other.id)

    def test_backfill_links_pre_existing_rows(self):
        # A call recorded before the contact existed.
        call = self._make_call("+14155557900", call_id="call-backfill")
        self.assertIsNone(call.contact_id)

        late_contact = Contact.objects.create(
            tenant=self.tenant, phone_number="+14155557900"
        )

        linked = contact_resolution.backfill_call_logs(tenant_id=self.tenant.id)
        self.assertEqual(linked, 1)

        call.refresh_from_db()
        self.assertEqual(call.contact_id, late_contact.id)

    def test_backfill_is_idempotent(self):
        self._make_call("+14155557001", call_id="call-idem")
        contact_resolution.backfill_call_logs(tenant_id=self.tenant.id)
        self.assertEqual(
            contact_resolution.backfill_call_logs(tenant_id=self.tenant.id), 0
        )

    def test_find_contact_returns_none_for_blank_input(self):
        self.assertIsNone(contact_resolution.find_contact(self.tenant.id, ""))
        self.assertIsNone(contact_resolution.find_contact(self.tenant.id, None))
        self.assertIsNone(contact_resolution.find_contact(None, "+14155557001"))
