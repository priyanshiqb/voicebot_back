"""
Caller and callee legs on a call log, and which of them is the person.

`caller_number` is the number that initiated the call and `callee_number` the one that
received it, so the customer swaps ends with the direction: they are the caller on
inbound and the callee on outbound.

Outbound rows exist in two eras. Before `callee_number` shipped, the runner put the
customer in `caller_number` regardless of direction, and those rows were left as they
were rather than rewritten. `AgentCallLog.customer_number` is the single place that
knows the difference — every test here is really about that property holding the line,
because contact resolution rides on it and would otherwise match outbound calls against
the bot's own number.
"""

from django.test import TestCase, override_settings

from core.models.agent import Agent
from core.models.agent_call_log import AgentCallLog
from core.models.contact import Contact
from core.services import contact_resolution
from tenant.models.Tenant import Tenant

BOT_DID = "+14155550100"
CUSTOMER = "+14155557001"


@override_settings(CONTACT_DEFAULT_PHONE_REGION="US")
class CustomerNumberTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-callee")
        self.agent = Agent.objects.create(
            tenant=self.tenant, bot_name="Bot", persona_name="P"
        )

    def _log(self, direction, caller, callee=None, call_id="call-1"):
        return AgentCallLog.objects.create(
            tenant=self.tenant,
            agent=self.agent,
            provider_call_id=call_id,
            caller_number=caller,
            callee_number=callee,
            direction=direction,
        )

    def test_inbound_customer_is_the_caller(self):
        log = self._log(
            AgentCallLog.CallDirection.INBOUND, caller=CUSTOMER, callee=BOT_DID
        )
        self.assertEqual(log.customer_number, CUSTOMER)

    def test_outbound_customer_is_the_callee(self):
        log = self._log(
            AgentCallLog.CallDirection.OUTBOUND, caller=BOT_DID, callee=CUSTOMER
        )
        self.assertEqual(log.customer_number, CUSTOMER)

    def test_outbound_falls_back_for_rows_predating_the_field(self):
        """The older era: customer sat in caller_number and callee_number is NULL."""
        log = self._log(
            AgentCallLog.CallDirection.OUTBOUND, caller=CUSTOMER, callee=None
        )
        self.assertEqual(log.customer_number, CUSTOMER)

    def test_empty_callee_falls_back_too(self):
        """A runner sending "" rather than omitting the key must not blank the person."""
        log = self._log(
            AgentCallLog.CallDirection.OUTBOUND, caller=CUSTOMER, callee=""
        )
        self.assertEqual(log.customer_number, CUSTOMER)

    def test_inbound_ignores_callee_entirely(self):
        """Even a wrong callee must not displace the inbound caller."""
        log = self._log(
            AgentCallLog.CallDirection.INBOUND, caller=CUSTOMER, callee="+19999999999"
        )
        self.assertEqual(log.customer_number, CUSTOMER)

    def test_str_names_the_person_not_the_leg(self):
        log = self._log(
            AgentCallLog.CallDirection.OUTBOUND, caller=BOT_DID, callee=CUSTOMER
        )
        self.assertIn(CUSTOMER, str(log))


@override_settings(CONTACT_DEFAULT_PHONE_REGION="US")
class ContactResolutionByDirectionTests(TestCase):
    """The reason customer_number exists: resolution must find the human."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-callee-res")
        self.agent = Agent.objects.create(
            tenant=self.tenant, bot_name="Bot", persona_name="P"
        )
        self.contact = Contact.objects.create(
            tenant=self.tenant, first_name="Ada", phone_number=CUSTOMER
        )

    def _log(self, direction, caller, callee=None, call_id="call-1"):
        return AgentCallLog.objects.create(
            tenant=self.tenant,
            agent=self.agent,
            provider_call_id=call_id,
            caller_number=caller,
            callee_number=callee,
            direction=direction,
        )

    def test_outbound_resolves_the_contact_from_the_callee(self):
        log = self._log(
            AgentCallLog.CallDirection.OUTBOUND, caller=BOT_DID, callee=CUSTOMER
        )
        self.assertEqual(log.contact_id, self.contact.id)

    def test_outbound_does_not_resolve_against_the_bot(self):
        """The failure this change prevents: a Contact holding the bot's own DID."""
        bot_contact = Contact.objects.create(
            tenant=self.tenant, first_name="Reception", phone_number=BOT_DID
        )
        log = self._log(
            AgentCallLog.CallDirection.OUTBOUND, caller=BOT_DID, callee=CUSTOMER
        )
        self.assertEqual(log.contact_id, self.contact.id)
        self.assertNotEqual(log.contact_id, bot_contact.id)

    def test_inbound_still_resolves_from_the_caller(self):
        log = self._log(
            AgentCallLog.CallDirection.INBOUND, caller=CUSTOMER, callee=BOT_DID
        )
        self.assertEqual(log.contact_id, self.contact.id)

    def test_legacy_outbound_row_still_resolves(self):
        """The two-era fallback, exercised through resolution rather than the property."""
        log = self._log(
            AgentCallLog.CallDirection.OUTBOUND, caller=CUSTOMER, callee=None
        )
        self.assertEqual(log.contact_id, self.contact.id)

    def test_resolution_is_normalized_on_the_callee_leg_too(self):
        """Formatting must not defeat the match on the new leg either."""
        log = self._log(
            AgentCallLog.CallDirection.OUTBOUND,
            caller=BOT_DID,
            callee="+1 (415) 555-7001",
        )
        self.assertEqual(log.contact_id, self.contact.id)

    def test_backfill_uses_the_callee_on_outbound(self):
        log = self._log(
            AgentCallLog.CallDirection.OUTBOUND, caller=BOT_DID, callee=CUSTOMER
        )
        # The pre_save signal links on create, so unlink first to give the backfill
        # something to do.
        AgentCallLog.objects.filter(pk=log.pk).update(contact=None)

        contact_resolution.backfill_call_logs(tenant_id=self.tenant.id)

        log.refresh_from_db()
        self.assertEqual(log.contact_id, self.contact.id)
