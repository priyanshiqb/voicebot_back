"""
CampaignMember: suppression and the one-row-per-person guarantee.

Suppression is the compliance surface. Every case here answers a question a regulator
can ask — "was this person called, and if not, why not" — so the tests assert on the
recorded reason, not merely that the row was excluded.
"""

from django.db import IntegrityError, transaction
from django.test import TestCase

from core.models.agent import Agent
from core.models.campaign import Campaign
from core.models.campaign_member import CampaignMember, suppression_reason_for
from core.models.contact import Contact
from tenant.models.Tenant import Tenant

Reason = CampaignMember.SuppressionReason


class SuppressionReasonTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-suppress")

    def _contact(self, phone="+14155552671", **kwargs):
        return Contact.objects.create(
            tenant=self.tenant, phone_number=phone, **kwargs
        )

    def test_ordinary_contact_is_not_suppressed(self):
        self.assertIsNone(suppression_reason_for(self._contact()))

    def test_do_not_call_suppresses(self):
        c = self._contact(do_not_call=True)
        self.assertEqual(suppression_reason_for(c), Reason.DO_NOT_CALL)

    def test_denied_consent_suppresses(self):
        c = self._contact(consent_status=Contact.ConsentStatus.DENIED)
        self.assertEqual(suppression_reason_for(c), Reason.CONSENT_DENIED)

    def test_withdrawn_consent_suppresses(self):
        c = self._contact(consent_status=Contact.ConsentStatus.WITHDRAWN)
        self.assertEqual(suppression_reason_for(c), Reason.CONSENT_DENIED)

    def test_unknown_consent_does_not_suppress(self):
        """
        Every contact starts UNKNOWN, so suppressing it would make every campaign call
        nobody. Locked in as a test because it is a policy decision, not an accident —
        if compliance rules otherwise, this test is the thing that should fail first.
        """
        c = self._contact()
        self.assertEqual(c.consent_status, Contact.ConsentStatus.UNKNOWN)
        self.assertIsNone(suppression_reason_for(c))

    def test_granted_consent_does_not_suppress(self):
        c = self._contact(consent_status=Contact.ConsentStatus.GRANTED)
        self.assertIsNone(suppression_reason_for(c))

    def test_deleted_contact_suppresses(self):
        c = self._contact()
        c.soft_delete()
        self.assertEqual(suppression_reason_for(c), Reason.CONTACT_DELETED)

    def test_missing_phone_suppresses(self):
        c = self._contact()
        # Bypass save() — the model derives phone_e164, so this is what a bad legacy
        # row looks like rather than something the API can produce.
        Contact.all_objects.filter(pk=c.pk).update(phone_e164="")
        c.refresh_from_db()
        self.assertEqual(suppression_reason_for(c), Reason.NO_PHONE)

    def test_do_not_call_wins_over_consent(self):
        """The more specific answer: they asked us not to call."""
        c = self._contact(
            do_not_call=True, consent_status=Contact.ConsentStatus.WITHDRAWN
        )
        self.assertEqual(suppression_reason_for(c), Reason.DO_NOT_CALL)

    def test_deletion_wins_over_everything(self):
        c = self._contact(do_not_call=True)
        c.soft_delete()
        self.assertEqual(suppression_reason_for(c), Reason.CONTACT_DELETED)


class CampaignMemberConstraintTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-member")
        self.agent = Agent.objects.create(
            tenant=self.tenant, bot_name="Bot", persona_name="P"
        )
        self.campaign = Campaign.objects.create(
            tenant=self.tenant, agent=self.agent, name="Members"
        )
        self.other_campaign = Campaign.objects.create(
            tenant=self.tenant, agent=self.agent, name="Other Members"
        )
        self.contact = Contact.objects.create(
            tenant=self.tenant, phone_number="+14155552671"
        )

    def _member(self, campaign=None, contact=None, **kwargs):
        contact = contact or self.contact
        return CampaignMember.objects.create(
            tenant=self.tenant,
            campaign=campaign or self.campaign,
            contact=contact,
            phone_e164=contact.phone_e164,
            **kwargs,
        )

    def test_one_member_per_contact_per_campaign(self):
        """The guard against dialling the same person twice from one campaign."""
        self._member()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._member()

    def test_same_contact_allowed_in_a_different_campaign(self):
        self._member()
        other = self._member(campaign=self.other_campaign)
        self.assertIsNotNone(other.pk)

    def test_soft_deleted_member_frees_the_slot(self):
        first = self._member()
        first.soft_delete()
        second = self._member()
        self.assertNotEqual(first.pk, second.pk)

    def test_phone_is_frozen_against_later_contact_edits(self):
        """
        The campaign must record what it handed over, not what the number became.
        """
        member = self._member()
        self.assertEqual(member.phone_e164, "+14155552671")

        self.contact.phone_number = "+14155552699"
        self.contact.save()

        member.refresh_from_db()
        self.assertEqual(member.phone_e164, "+14155552671")

    def test_contact_is_protected_from_hard_delete(self):
        """PROTECT: erasing a contact must not silently shred campaign history."""
        self._member()
        with self.assertRaises(Exception):
            with transaction.atomic():
                Contact.all_objects.filter(pk=self.contact.pk).delete()

    def test_default_status_is_pending(self):
        member = self._member()
        self.assertEqual(member.status, CampaignMember.Status.PENDING)
        self.assertEqual(member.suppression_reason, "")
        self.assertEqual(member.attempt_count, 0)


class CampaignMemberPropertyTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-member-prop")
        self.agent = Agent.objects.create(
            tenant=self.tenant, bot_name="Bot", persona_name="P"
        )
        self.campaign = Campaign.objects.create(
            tenant=self.tenant, agent=self.agent, name="Props"
        )

    def _member(self, phone, **kwargs):
        contact = Contact.objects.create(tenant=self.tenant, phone_number=phone)
        return CampaignMember.objects.create(
            tenant=self.tenant,
            campaign=self.campaign,
            contact=contact,
            phone_e164=contact.phone_e164,
            **kwargs,
        )

    def test_suppressed_members_are_not_handed_off(self):
        member = self._member(
            "+14155552671",
            status=CampaignMember.Status.SUPPRESSED,
            suppression_reason=Reason.DO_NOT_CALL,
        )
        self.assertTrue(member.is_suppressed)
        self.assertFalse(member.was_handed_off)
        self.assertTrue(member.is_terminal)

    def test_pending_members_are_handed_off(self):
        member = self._member("+14155552672")
        self.assertTrue(member.was_handed_off)
        self.assertFalse(member.is_terminal)
        self.assertFalse(member.was_contacted)

    def test_completed_counts_as_contacted(self):
        member = self._member("+14155552673", status=CampaignMember.Status.COMPLETED)
        self.assertTrue(member.was_contacted)
        self.assertTrue(member.is_terminal)

    def test_failed_does_not_count_as_contacted(self):
        """Tried three times and gave up is not the same as reached."""
        member = self._member(
            "+14155552674", status=CampaignMember.Status.FAILED, attempt_count=3
        )
        self.assertFalse(member.was_contacted)
        self.assertTrue(member.is_terminal)

    def test_in_progress_is_neither_terminal_nor_contacted(self):
        member = self._member(
            "+14155552675", status=CampaignMember.Status.IN_PROGRESS
        )
        self.assertFalse(member.is_terminal)
        self.assertFalse(member.was_contacted)

    def test_status_ownership_sets_do_not_overlap(self):
        """
        Our statuses and the runner's must stay disjoint — if they ever overlap, "who
        wrote this" becomes unanswerable during an incident.
        """
        self.assertEqual(
            set(CampaignMember.LOCAL_STATUSES) & set(CampaignMember.RUNNER_STATUSES),
            set(),
        )
