"""
Campaign model: name validation, the case-insensitive partial unique constraint, and
schedule derivation.

The scheduling tests carry the most weight. A campaign start is a date and a time in
somebody else's timezone, and every cheap way of checking "is this in the past" is wrong
in a way that only shows up for users in particular zones at particular hours.
"""

from datetime import date, datetime, time
from unittest import mock
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from core.models.agent import Agent
from core.models.campaign import (
    Campaign,
    combine_local,
    resolve_tenant_timezone,
)
from core.models.campaign_member import CampaignMember
from core.models.contact import Contact
from onboard.models.timezone import Timezone
from tenant.models.Tenant import Tenant

UTC = ZoneInfo("UTC")


def make_agent(tenant, name="Sales Bot"):
    return Agent.objects.create(
        tenant=tenant, bot_name=name, persona_name="Persona"
    )


class CampaignValidationTests(TestCase):
    """Model validators only fire through full_clean(), so that is what these call."""

    # BaseModel declares created_by / updated_by / deleted_by as null=True but not
    # blank=True, so full_clean() demands them even though BaseViewSet.perform_create
    # is what supplies them and the model never does. Excluded so these tests assert on
    # campaign validation rather than on that base-class quirk.
    AUDIT_FIELDS = ["created_by", "updated_by", "deleted_by"]

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-camp-val")
        self.agent = make_agent(self.tenant)

    def _campaign(self, **kwargs):
        defaults = {"tenant": self.tenant, "agent": self.agent, "name": "Q1 Leads"}
        return Campaign(**{**defaults, **kwargs})

    def _clean(self, **kwargs):
        self._campaign(**kwargs).full_clean(exclude=self.AUDIT_FIELDS)

    def test_valid_name_passes(self):
        self._clean(name="Q1 Lead-Qual_2024")

    def test_name_too_short(self):
        with self.assertRaises(ValidationError) as ctx:
            self._clean(name="Q1")
        self.assertIn("name", ctx.exception.error_dict)

    def test_name_too_long(self):
        with self.assertRaises(ValidationError) as ctx:
            self._clean(name="x" * 101)
        self.assertIn("name", ctx.exception.error_dict)

    def test_name_rejects_punctuation(self):
        for bad in ["Q1 Lead!", "Q1@Leads", "Q1/Leads", "Q1.Leads"]:
            with self.subTest(name=bad):
                with self.assertRaises(ValidationError):
                    self._clean(name=bad)

    def test_name_allows_letters_digits_space_hyphen_underscore(self):
        for good in ["Q1 Leads", "Q1-Leads", "Q1_Leads", "Campaign 2024"]:
            with self.subTest(name=good):
                self._clean(name=good)

    def test_description_may_be_blank(self):
        """
        Blank short-circuits before validators run — "" is in Field.empty_values — so
        MinLengthValidator(3) does not fire on an omitted description. Asserted here so
        nobody "fixes" the optional-but-min-length combination into something worse.
        """
        self._clean(description="")

    def test_description_too_short(self):
        with self.assertRaises(ValidationError) as ctx:
            self._clean(description="ab")
        self.assertIn("description", ctx.exception.error_dict)

    def test_description_too_long(self):
        with self.assertRaises(ValidationError) as ctx:
            self._clean(description="x" * 501)
        self.assertIn("description", ctx.exception.error_dict)

    def test_defaults(self):
        c = Campaign.objects.create(
            tenant=self.tenant, agent=self.agent, name="Defaults"
        )
        self.assertEqual(c.status, Campaign.Status.DRAFT)
        self.assertEqual(c.audience_source, Campaign.AudienceSource.SELECTED)
        self.assertEqual(c.handoff_status, Campaign.HandoffStatus.PENDING)
        self.assertIsNone(c.scheduled_start_at)
        self.assertEqual(c.schedule_timezone, "")
        self.assertTrue(c.is_open)
        self.assertFalse(c.is_live)
        self.assertFalse(c.is_terminal)


class CampaignUniquenessTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-camp-uniq")
        self.other = Tenant.objects.create(name="Globex", subdomain="globex-camp-uniq")
        self.agent = make_agent(self.tenant)
        self.other_agent = make_agent(self.other, name="Other Bot")

    def test_duplicate_name_blocked(self):
        Campaign.objects.create(tenant=self.tenant, agent=self.agent, name="Q1 Leads")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Campaign.objects.create(
                    tenant=self.tenant, agent=self.agent, name="Q1 Leads"
                )

    def test_duplicate_name_differing_only_by_case_blocked(self):
        """The whole point of the Lower() constraint — a user cannot see this difference."""
        Campaign.objects.create(tenant=self.tenant, agent=self.agent, name="Q1 Leads")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Campaign.objects.create(
                    tenant=self.tenant, agent=self.agent, name="q1 LEADS"
                )

    def test_same_name_allowed_across_tenants(self):
        Campaign.objects.create(tenant=self.tenant, agent=self.agent, name="Q1 Leads")
        other = Campaign.objects.create(
            tenant=self.other, agent=self.other_agent, name="Q1 Leads"
        )
        self.assertIsNotNone(other.pk)

    def test_soft_delete_frees_the_name(self):
        first = Campaign.objects.create(
            tenant=self.tenant, agent=self.agent, name="Q1 Leads"
        )
        first.soft_delete()

        second = Campaign.objects.create(
            tenant=self.tenant, agent=self.agent, name="Q1 Leads"
        )
        self.assertNotEqual(first.pk, second.pk)


class TenantTimezoneResolutionTests(TestCase):
    def test_falls_back_to_utc_when_unset(self):
        tenant = Tenant.objects.create(name="Acme", subdomain="acme-tz-none")
        self.assertEqual(resolve_tenant_timezone(tenant), "UTC")

    def test_uses_tenant_iana_name(self):
        tz = Timezone.objects.create(
            offset="UTC+05:30", region="Kolkata", timezone="Asia/Kolkata"
        )
        tenant = Tenant.objects.create(
            name="Acme", subdomain="acme-tz-ist", timezone=tz
        )
        self.assertEqual(resolve_tenant_timezone(tenant), "Asia/Kolkata")

    def test_unparseable_zone_falls_back_rather_than_raising(self):
        """A bad seed row must not make every campaign in that tenant uncreatable."""
        tz = Timezone.objects.create(
            offset="UTC+00:00", region="Nowhere", timezone="Not/AZone"
        )
        tenant = Tenant.objects.create(
            name="Acme", subdomain="acme-tz-bad", timezone=tz
        )
        self.assertEqual(resolve_tenant_timezone(tenant), "UTC")


class CombineLocalTests(TestCase):
    def test_local_wall_clock_converts_to_utc(self):
        got = combine_local(date(2027, 1, 15), time(9, 0), "Asia/Kolkata")
        self.assertEqual(got, datetime(2027, 1, 15, 3, 30, tzinfo=UTC))

    def test_local_date_can_roll_back_a_utc_day(self):
        """02:00 IST is the previous day in UTC — the case a UTC-date check gets wrong."""
        got = combine_local(date(2027, 1, 15), time(2, 0), "Asia/Kolkata")
        self.assertEqual(got, datetime(2027, 1, 14, 20, 30, tzinfo=UTC))

    def test_local_date_can_roll_forward_a_utc_day(self):
        got = combine_local(date(2027, 1, 14), time(20, 0), "America/Los_Angeles")
        self.assertEqual(got, datetime(2027, 1, 15, 4, 0, tzinfo=UTC))

    def test_dst_spring_forward_resolves_rather_than_raising(self):
        """
        02:30 does not exist in New York on 2027-03-14. ZoneInfo resolves it instead of
        raising; a campaign is not worth failing over a one-hour-a-year edge, and the
        stored UTC instant records which side was picked.
        """
        got = combine_local(date(2027, 3, 14), time(2, 30), "America/New_York")
        self.assertIsNotNone(got)
        self.assertEqual(got.tzinfo, UTC)


class CampaignScheduleTests(TestCase):
    """
    `now` is pinned so these assert behaviour rather than the wall clock.

    The pinned instant is deliberately one where the UTC date and the tenant's local
    date disagree: 2027-01-15 02:00 UTC is still 2027-01-14 18:00 in Los Angeles.
    """

    PINNED_NOW = datetime(2027, 1, 15, 2, 0, tzinfo=UTC)

    def setUp(self):
        self.tz = Timezone.objects.create(
            offset="UTC-08:00", region="Los Angeles", timezone="America/Los_Angeles"
        )
        self.tenant = Tenant.objects.create(
            name="Acme", subdomain="acme-camp-sched", timezone=self.tz
        )
        self.agent = make_agent(self.tenant)
        self.campaign = Campaign(
            tenant=self.tenant, agent=self.agent, name="Scheduled"
        )

    def _pin_now(self):
        return mock.patch(
            "core.models.campaign.timezone.now", return_value=self.PINNED_NOW
        )

    def test_sets_utc_instant_and_records_the_zone(self):
        with self._pin_now():
            self.campaign.set_schedule(date(2027, 1, 20), time(9, 0))
        self.assertEqual(
            self.campaign.scheduled_start_at, datetime(2027, 1, 20, 17, 0, tzinfo=UTC)
        )
        self.assertEqual(self.campaign.schedule_timezone, "America/Los_Angeles")

    def test_local_start_round_trips_what_the_user_typed(self):
        with self._pin_now():
            self.campaign.set_schedule(date(2027, 1, 20), time(9, 0))
        local = self.campaign.local_start
        self.assertEqual(local.date(), date(2027, 1, 20))
        self.assertEqual(local.hour, 9)
        self.assertEqual(local.minute, 0)

    def test_today_local_but_later_today_is_accepted(self):
        """
        Local date is 2027-01-14 while the UTC date is already 2027-01-15. A naive
        "is the date >= today (UTC)" check rejects this legal future start.
        """
        with self._pin_now():
            self.campaign.set_schedule(date(2027, 1, 14), time(20, 0))
        self.assertEqual(
            self.campaign.scheduled_start_at, datetime(2027, 1, 15, 4, 0, tzinfo=UTC)
        )

    def test_today_local_but_earlier_today_is_rejected(self):
        """
        Same local date, a time that has already passed. A date-only check waves this
        through and the campaign fires immediately.
        """
        with self._pin_now():
            with self.assertRaises(ValidationError) as ctx:
                self.campaign.set_schedule(date(2027, 1, 14), time(10, 0))
        self.assertIn("start_date", ctx.exception.error_dict)

    def test_past_date_rejected(self):
        with self._pin_now():
            with self.assertRaises(ValidationError):
                self.campaign.set_schedule(date(2026, 12, 1), time(9, 0))

    def test_date_without_time_rejected(self):
        with self._pin_now():
            with self.assertRaises(ValidationError) as ctx:
                self.campaign.set_schedule(date(2027, 1, 20), None)
        self.assertIn("start_time", ctx.exception.error_dict)

    def test_time_without_date_rejected(self):
        with self._pin_now():
            with self.assertRaises(ValidationError):
                self.campaign.set_schedule(None, time(9, 0))

    def test_neither_clears_the_schedule(self):
        with self._pin_now():
            self.campaign.set_schedule(date(2027, 1, 20), time(9, 0))
            self.campaign.set_schedule(None, None)
        self.assertIsNone(self.campaign.scheduled_start_at)
        self.assertEqual(self.campaign.schedule_timezone, "")

    def test_explicit_zone_overrides_the_tenant(self):
        with self._pin_now():
            self.campaign.set_schedule(
                date(2027, 1, 20), time(9, 0), tz_name="Asia/Kolkata"
            )
        self.assertEqual(self.campaign.schedule_timezone, "Asia/Kolkata")
        self.assertEqual(
            self.campaign.scheduled_start_at, datetime(2027, 1, 20, 3, 30, tzinfo=UTC)
        )

    def test_stored_instant_is_immune_to_a_later_tenant_zone_change(self):
        """
        The reason the UTC instant is stored rather than a date/time pair: moving the
        tenant must not silently move every already-scheduled campaign.
        """
        with self._pin_now():
            self.campaign.set_schedule(date(2027, 1, 20), time(9, 0))
        self.campaign.save()
        original = self.campaign.scheduled_start_at

        ist = Timezone.objects.create(
            offset="UTC+05:30", region="Kolkata", timezone="Asia/Kolkata"
        )
        self.tenant.timezone = ist
        self.tenant.save(update_fields=["timezone"])

        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.scheduled_start_at, original)
        self.assertEqual(self.campaign.schedule_timezone, "America/Los_Angeles")


class CampaignCounterTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-camp-count")
        self.agent = make_agent(self.tenant)
        self.campaign = Campaign.objects.create(
            tenant=self.tenant, agent=self.agent, name="Counted"
        )

    def _member(self, phone, status):
        contact = Contact.objects.create(tenant=self.tenant, phone_number=phone)
        return CampaignMember.objects.create(
            tenant=self.tenant,
            campaign=self.campaign,
            contact=contact,
            phone_e164=contact.phone_e164,
            status=status,
        )

    def test_recompute_counts_from_members(self):
        self._member("+14155552671", CampaignMember.Status.PENDING)
        self._member("+14155552672", CampaignMember.Status.PENDING)
        self._member("+14155552673", CampaignMember.Status.SUPPRESSED)
        self._member("+14155552674", CampaignMember.Status.COMPLETED)
        self._member("+14155552675", CampaignMember.Status.FAILED)

        self.campaign.recompute_counts()
        self.campaign.refresh_from_db()

        self.assertEqual(self.campaign.total_members, 5)
        self.assertEqual(self.campaign.pending_count, 2)
        self.assertEqual(self.campaign.suppressed_count, 1)
        self.assertEqual(self.campaign.completed_count, 1)
        self.assertEqual(self.campaign.failed_count, 1)

    def test_recompute_counts_is_repeatable(self):
        """Drift repair must be idempotent, not cumulative."""
        self._member("+14155552671", CampaignMember.Status.PENDING)
        self.campaign.recompute_counts()
        self.campaign.recompute_counts()
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.total_members, 1)
        self.assertEqual(self.campaign.pending_count, 1)

    def test_counts_are_zero_with_no_members(self):
        self.campaign.recompute_counts()
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.total_members, 0)
