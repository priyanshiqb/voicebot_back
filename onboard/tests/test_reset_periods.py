"""Windowed allowances — the configurable reset period.

Two things are under test here, and they were two separate defects.

**The window used to be the calendar month**, while a billing cycle starts whenever
the tenant subscribed. A cycle running 5 Aug to 4 Sep therefore contained *two*
campaign allowances, and a yearly cycle contained twelve. Windows are now anchored to
the subscription, so each one sits wholly inside a cycle.

**The count used to come from the default manager**, which hides soft-deleted rows —
so a tenant could run their month's campaigns, delete them, and run the same number
again, indefinitely. It now counts campaigns that *started*, from ``all_objects``, and
a campaign that started cannot be deleted at all.
"""

from datetime import datetime, timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from core.models.campaign import Campaign
from onboard.constants.entitlement_resource import EntitlementResource
from onboard.constants.reset_period import ResetPeriod, is_windowed, window_start
from onboard.models import Subscription
from onboard.services import entitlements as entitlement_service
from onboard.services import subscriptions as subscription_service
from onboard.tests.billing_factories import (
    make_agent,
    make_campaign,
    make_plan,
    make_tenant,
)
from tenant.models import TenantSettings

Resource = EntitlementResource


def aware(year, month, day, hour=0):
    return timezone.make_aware(datetime(year, month, day, hour, 0))


class WindowArithmeticTests(TestCase):
    """``window_start`` alone — no database, no subscription."""

    def test_a_standing_total_has_no_window(self):
        """The default. Nothing is counted from a date."""
        self.assertFalse(is_windowed(ResetPeriod.NONE))
        self.assertIsNone(
            window_start(ResetPeriod.NONE, aware(2027, 1, 1), aware(2027, 6, 1))
        )

    def test_every_period_is_offered(self):
        """Six windows plus 'never', because a rate limit and a quota are set the
        same way and only the period differs."""
        self.assertEqual(
            [choice.value for choice in ResetPeriod],
            ["none", "daily", "weekly", "monthly", "quarterly", "half_yearly", "yearly"],
        )

    def test_daily_and_weekly_step_in_fixed_spans(self):
        """Anchored at the subscription's own time of day, not midnight — a window
        that opened at 14:00 closes at 14:00."""
        anchor = aware(2027, 3, 1, 14)

        self.assertEqual(
            window_start(ResetPeriod.DAILY, anchor, aware(2027, 3, 10, 20)),
            aware(2027, 3, 10, 14),
        )
        self.assertEqual(
            window_start(ResetPeriod.WEEKLY, anchor, aware(2027, 3, 20, 9)),
            aware(2027, 3, 15, 14),
        )

    def test_monthly_windows_follow_the_calendar_not_thirty_days(self):
        """A window that opens on the 31st closes at the end of February, the same
        way a billing cycle does. Thirty-day steps would drift out of alignment
        within a quarter."""
        anchor = aware(2027, 1, 31)

        self.assertEqual(
            window_start(ResetPeriod.MONTHLY, anchor, aware(2027, 3, 5)),
            aware(2027, 2, 28),
        )

    def test_longer_periods_span_several_billing_cycles(self):
        """**The reason the anchor must not move at renewal.**

        A quarterly allowance on a monthly plan is meant to cover three cycles. If it
        were anchored to the current cycle it would reset every month and quietly
        become a monthly allowance.
        """
        anchor = aware(2027, 1, 15)

        for period, expected in [
            (ResetPeriod.QUARTERLY, aware(2027, 4, 15)),
            (ResetPeriod.HALF_YEARLY, aware(2027, 1, 15)),
            (ResetPeriod.YEARLY, aware(2027, 1, 15)),
        ]:
            self.assertEqual(
                window_start(period, anchor, aware(2027, 6, 20)), expected, period
            )

    def test_the_first_window_opens_at_the_anchor(self):
        """Before any period has elapsed, and defensively for an anchor in the
        future, the window is the anchor itself — never a date before the tenant
        existed."""
        anchor = aware(2027, 5, 1)

        self.assertEqual(
            window_start(ResetPeriod.MONTHLY, anchor, aware(2027, 5, 3)), anchor
        )
        self.assertEqual(
            window_start(ResetPeriod.MONTHLY, anchor, aware(2027, 4, 1)), anchor
        )


class CampaignWindowTests(TestCase):
    """The window as a tenant experiences it."""

    def setUp(self):
        self.ctx = make_tenant("window")
        self.tenant = self.ctx.tenant

    def subscribe(self, *, period, included=3, cycle="monthly"):
        plan = make_plan(f"window-{period}-{cycle}", billing_cycle=cycle)
        plan.entitlements.filter(resource=Resource.CAMPAIGN_MONTHLY).update(
            included_quantity=included, reset_period=period
        )
        subscription = subscription_service.activate(self.tenant, plan)
        subscription.generate_plan_snapshot()
        subscription.save(update_fields=["plan_snapshot"])
        self.agent = make_agent(self.tenant)
        return subscription

    def run_campaigns(self, count, *, when=None):
        """Campaigns created and started at ``when``.

        Both dates are set: the allowance is counted from creation, so a campaign
        that ran last month has to have been *created* last month too.
        """
        when = when or timezone.now()
        started = []
        for _ in range(count):
            campaign = make_campaign(self.tenant, self.agent)
            Campaign.objects.filter(pk=campaign.pk).update(
                status=Campaign.Status.COMPLETED,
                started_at=when,
                created_at=when,
            )
            campaign.refresh_from_db()
            started.append(campaign)
        return started

    def used(self, subscription):
        return entitlement_service.current_usage(
            self.tenant.id, Resource.CAMPAIGN_MONTHLY, subscription=subscription
        )

    def test_the_window_is_frozen_onto_the_subscription(self):
        """**The number is meaningless without the period.**

        Ten a month and ten a year are the same figure. Freezing only the number
        would mean changing a plan's period silently re-interpreted every
        subscription already sold.
        """
        subscription = self.subscribe(period=ResetPeriod.MONTHLY)

        entry = subscription.plan_snapshot["entitlements"][Resource.CAMPAIGN_MONTHLY]
        self.assertEqual(entry["included"], 3)
        self.assertEqual(entry["reset_period"], ResetPeriod.MONTHLY)
        self.assertEqual(
            subscription.entitlement_reset_period(Resource.CAMPAIGN_MONTHLY),
            ResetPeriod.MONTHLY,
        )

    def test_a_plan_whose_period_changes_later_does_not_move_an_existing_tenant(self):
        """The same freeze guarantee the price and the credit rules already have."""
        subscription = self.subscribe(period=ResetPeriod.MONTHLY)

        subscription.plan.entitlements.filter(
            resource=Resource.CAMPAIGN_MONTHLY
        ).update(reset_period=ResetPeriod.YEARLY)

        self.assertEqual(
            subscription.entitlement_reset_period(Resource.CAMPAIGN_MONTHLY),
            ResetPeriod.MONTHLY,
        )

    def test_an_older_snapshot_without_a_period_still_works(self):
        """Subscriptions written before this field existed store a bare number.
        They read the plan rather than breaking."""
        subscription = self.subscribe(period=ResetPeriod.MONTHLY)
        subscription.plan_snapshot["entitlements"][Resource.CAMPAIGN_MONTHLY] = 3
        subscription.save(update_fields=["plan_snapshot"])

        self.assertEqual(
            subscription.included_entitlement(Resource.CAMPAIGN_MONTHLY), 3
        )
        self.assertEqual(
            subscription.entitlement_reset_period(Resource.CAMPAIGN_MONTHLY),
            ResetPeriod.MONTHLY,
        )

    def test_the_window_no_longer_resets_inside_a_billing_cycle(self):
        """**The defect this replaced.**

        The counter reset on the 1st while the cycle ran from whenever the tenant
        subscribed, so a cycle straddling a month boundary contained two allowances.
        Anchored to the subscription, campaigns run earlier in the same cycle still
        count.
        """
        subscription = self.subscribe(period=ResetPeriod.MONTHLY)
        Subscription.objects.filter(pk=subscription.pk).update(
            created_at=timezone.now() - timedelta(days=20)
        )
        subscription.refresh_from_db()

        # Run three a fortnight ago — before the 1st of this calendar month would
        # have wiped them, but inside this subscription's own window.
        self.run_campaigns(3, when=timezone.now() - timedelta(days=14))

        self.assertEqual(self.used(subscription), 3)

    def test_a_previous_window_does_not_count(self):
        """The allowance does refill — just on the tenant's own clock."""
        subscription = self.subscribe(period=ResetPeriod.MONTHLY)
        Subscription.objects.filter(pk=subscription.pk).update(
            created_at=timezone.now() - timedelta(days=200)
        )
        subscription.refresh_from_db()

        self.run_campaigns(3, when=timezone.now() - timedelta(days=45))

        self.assertEqual(self.used(subscription), 0)

    def test_a_yearly_window_holds_the_whole_year(self):
        """One bucket per cycle on an annual plan — the alternative to twelve."""
        subscription = self.subscribe(
            period=ResetPeriod.YEARLY, included=120, cycle="yearly"
        )
        Subscription.objects.filter(pk=subscription.pk).update(
            created_at=timezone.now() - timedelta(days=200)
        )
        subscription.refresh_from_db()

        self.run_campaigns(2, when=timezone.now() - timedelta(days=150))
        self.run_campaigns(1)

        self.assertEqual(self.used(subscription), 3)

    def test_a_daily_window_is_a_rate_limit(self):
        """The same mechanism set fast enough to pace a tenant rather than cap
        their month."""
        subscription = self.subscribe(period=ResetPeriod.DAILY, included=2)
        Subscription.objects.filter(pk=subscription.pk).update(
            created_at=timezone.now() - timedelta(days=30)
        )
        subscription.refresh_from_db()

        self.run_campaigns(5, when=timezone.now() - timedelta(days=2))
        self.assertEqual(self.used(subscription), 0)

        self.run_campaigns(2)
        self.assertEqual(self.used(subscription), 2)


class DeletionCannotReclaimAllowanceTests(TestCase):
    """**The hole this closed.**

    ``current_usage`` counted through the default manager, which hides soft-deleted
    rows. A tenant on three campaigns a month could run three, delete them, and run
    three more — indefinitely, at no cost.
    """

    def setUp(self):
        self.ctx = make_tenant("reclaim")
        self.tenant = self.ctx.tenant
        self.plan = make_plan("reclaim-plan")
        self.plan.entitlements.filter(resource=Resource.CAMPAIGN_MONTHLY).update(
            included_quantity=3, reset_period=ResetPeriod.MONTHLY
        )
        self.subscription = subscription_service.activate(self.tenant, self.plan)
        self.subscription.generate_plan_snapshot()
        self.subscription.save(update_fields=["plan_snapshot"])
        self.agent = make_agent(self.tenant)

    def run_one(self):
        campaign = make_campaign(self.tenant, self.agent)
        Campaign.objects.filter(pk=campaign.pk).update(
            status=Campaign.Status.COMPLETED, started_at=timezone.now()
        )
        campaign.refresh_from_db()
        return campaign

    def draft(self):
        return make_campaign(self.tenant, self.agent)

    def used(self):
        return entitlement_service.current_usage(
            self.tenant.id, Resource.CAMPAIGN_MONTHLY, subscription=self.subscription
        )

    def test_a_campaign_that_ran_cannot_be_deleted(self):
        """It is a billing record. Deleting it would hand the allowance back."""
        campaign = self.run_one()

        with self.assertRaises(ValidationError) as caught:
            campaign.soft_delete()

        self.assertIn("already run", " ".join(caught.exception.messages))
        campaign.refresh_from_db()
        self.assertFalse(campaign.is_deleted)

    def test_an_unstarted_draft_counts_until_it_is_thrown_away(self):
        """**The limit is felt when a campaign is set up**, so a live draft counts —
        a tenant should learn they are at their limit before building an audience.

        Discarding it gives the allowance back, because nothing was ever dialled.
        That exemption is keyed on the campaign never having run, which is what stops
        it becoming a create-and-delete loop.
        """
        draft = make_campaign(self.tenant, self.agent)
        self.assertEqual(self.used(), 1)

        draft.soft_delete()

        draft.refresh_from_db()
        self.assertTrue(draft.is_deleted)
        self.assertEqual(self.used(), 0)

    # -- Never ran: the slot comes back, whichever way it ended ------------ #

    def test_cancelling_before_it_starts_gives_the_slot_back(self):
        """The tenant changed their mind before a single call went out.

        Cancel leaves ``is_deleted`` False, so a rule keyed only on deletion would
        have kept charging for it — the campaign is gone from the tenant's point of
        view and still holding an allowance. The rule is keyed on ``started_at``
        instead, so all three ways of never running behave the same.
        """
        campaign = self.draft()
        self.assertEqual(self.used(), 1)

        Campaign.objects.filter(pk=campaign.pk).update(
            status=Campaign.Status.CANCELLED
        )

        campaign.refresh_from_db()
        self.assertFalse(campaign.is_deleted)
        self.assertIsNone(campaign.started_at)
        self.assertEqual(self.used(), 0)

    def test_cancelling_after_it_started_does_not(self):
        """Half a campaign is still a campaign. Those contacts cannot be un-called."""
        campaign = self.run_one()
        self.assertEqual(self.used(), 1)

        Campaign.objects.filter(pk=campaign.pk).update(
            status=Campaign.Status.CANCELLED
        )

        campaign.refresh_from_db()
        self.assertIsNotNone(campaign.started_at)
        self.assertEqual(self.used(), 1)

    def test_failing_to_materialise_gives_the_slot_back(self):
        """The bot was deleted, or the audience resolved to nobody callable.

        Nothing was dialled and the failure was not the tenant's doing, so charging
        an allowance for it would be indefensible — and before this it did, until
        the tenant noticed and deleted the row by hand.
        """
        campaign = self.draft()
        self.assertEqual(self.used(), 1)

        Campaign.objects.filter(pk=campaign.pk).update(
            status=Campaign.Status.FAILED, error_message="no callable contacts"
        )

        self.assertEqual(self.used(), 0)

    def test_a_refunded_slot_is_not_free_re_entry(self):
        """The refund is not a loophole: taking the slot back means passing the
        check again. A tenant at their limit who cancels one campaign can start
        one more, not two."""
        row = TenantSettings.for_tenant(self.tenant.id)
        row.entitlements_enforced = True
        row.save(update_fields=["entitlements_enforced"])

        campaigns = [self.draft() for _ in range(3)]
        self.assertEqual(self.used(), 3)
        with self.assertRaises(entitlement_service.QuotaExceeded):
            entitlement_service.check(self.tenant, Resource.CAMPAIGN_MONTHLY)

        Campaign.objects.filter(pk=campaigns[0].pk).update(
            status=Campaign.Status.CANCELLED
        )

        self.assertEqual(self.used(), 2)
        entitlement_service.check(self.tenant, Resource.CAMPAIGN_MONTHLY)

        self.draft()
        self.assertEqual(self.used(), 3)
        with self.assertRaises(entitlement_service.QuotaExceeded):
            entitlement_service.check(self.tenant, Resource.CAMPAIGN_MONTHLY)

    def test_a_live_campaign_still_counts_while_it_is_mid_flight(self):
        """MATERIALIZING has no ``started_at`` yet and is not terminal, so it must
        keep its slot — otherwise the window would briefly under-count every
        campaign between launch and the runner picking it up."""
        campaign = self.draft()
        Campaign.objects.filter(pk=campaign.pk).update(
            status=Campaign.Status.MATERIALIZING
        )

        campaign.refresh_from_db()
        self.assertIsNone(campaign.started_at)
        self.assertEqual(self.used(), 1)

    # -- The table has to survive as a billing record ----------------------- #

    def test_campaign_rows_cannot_be_bulk_hard_deleted(self):
        """``all_objects`` is a plain Manager on the base model, so ``.delete()``
        on it was a real DELETE that raised nothing — and would have erased the
        ``started_at`` history the whole allowance rests on."""
        self.run_one()

        with self.assertRaises(Exception) as caught:
            Campaign.all_objects.filter(tenant_id=self.tenant.id).delete()

        self.assertIn("Bulk delete is not allowed", str(caught.exception))
        self.assertEqual(self.used(), 1)

    def test_declaring_all_objects_did_not_promote_it_to_default(self):
        """``_default_manager`` is what the admin, related lookups and dumpdata read
        through. Django promotes the newest manager unless told otherwise, which
        would have leaked soft-deleted campaigns into all three."""
        self.assertEqual(Campaign._default_manager.name, "objects")

        draft = self.draft()
        draft.soft_delete()

        self.assertFalse(Campaign._default_manager.filter(pk=draft.pk).exists())
        self.assertTrue(Campaign.all_objects.filter(pk=draft.pk).exists())

    def test_the_allowance_survives_a_deletion_attempt_on_every_route(self):
        """``bulk_delete`` calls ``soft_delete()`` directly, so the guard lives on
        the model rather than the viewset."""
        campaigns = [self.run_one() for _ in range(3)]
        self.assertEqual(self.used(), 3)

        for campaign in campaigns:
            with self.assertRaises(ValidationError):
                campaign.soft_delete()

        self.assertEqual(self.used(), 3)

    def test_even_a_row_forced_deleted_still_counts(self):
        """Belt and braces. If a row is soft-deleted by some path that bypasses the
        model — a data fix, a bulk update — the count is taken from
        ``all_objects`` and is unaffected."""
        campaigns = [self.run_one() for _ in range(3)]
        Campaign.objects.filter(
            pk__in=[c.pk for c in campaigns]
        ).update(is_deleted=True)

        self.assertEqual(self.used(), 3)


class StartTimeEnforcementTests(TestCase):
    """The allowance is checked where the act happens."""

    def setUp(self):
        self.ctx = make_tenant("start-guard")
        self.tenant = self.ctx.tenant
        self.plan = make_plan("start-guard-plan", allocated_credits=10000)
        self.plan.entitlements.filter(resource=Resource.CAMPAIGN_MONTHLY).update(
            included_quantity=2, reset_period=ResetPeriod.MONTHLY
        )
        self.subscription = subscription_service.activate(self.tenant, self.plan)
        self.subscription.generate_plan_snapshot()
        self.subscription.save(update_fields=["plan_snapshot"])
        self.agent = make_agent(self.tenant)

    def enforce(self):
        row = TenantSettings.for_tenant(self.tenant.id)
        row.entitlements_enforced = True
        row.save(update_fields=["entitlements_enforced"])

    def scheduled(self):
        return Campaign.objects.create(
            tenant=self.tenant, agent=self.agent,
            name=f"start guard {timezone.now().timestamp()}".replace(".", ""),
            status=Campaign.Status.SCHEDULED,
            scheduled_start_at=timezone.now() - timedelta(minutes=5),
        )

    def test_a_campaign_over_the_allowance_does_not_start(self):
        """**The authoritative check.** A campaign scheduled last month must not
        start this month without being counted against either window."""
        from core.services import campaigns as campaign_service

        self.enforce()
        for _ in range(2):
            campaign = self.scheduled()
            Campaign.objects.filter(pk=campaign.pk).update(
                status=Campaign.Status.COMPLETED, started_at=timezone.now()
            )
        blocked = self.scheduled()

        with self.assertLogs("core.services.campaigns", level="WARNING"):
            campaign_service.start_due_campaigns()

        blocked.refresh_from_db()
        self.assertEqual(blocked.status, Campaign.Status.SCHEDULED)

    def test_it_only_logs_while_enforcement_is_off(self):
        """Metering before blocking, the same as every other limit."""
        from core.services import campaigns as campaign_service

        for _ in range(5):
            campaign = self.scheduled()
            Campaign.objects.filter(pk=campaign.pk).update(
                status=Campaign.Status.COMPLETED, started_at=timezone.now()
            )
        allowed = self.scheduled()

        with self.assertLogs("onboard.services.entitlements", level="WARNING"):
            campaign_service.start_due_campaigns()

        allowed.refresh_from_db()
        self.assertEqual(allowed.status, Campaign.Status.MATERIALIZING)

    def test_a_broken_allowance_check_never_stops_a_campaign(self):
        """Fail open, exactly as the credit pre-flight does."""
        from unittest.mock import patch

        from core.services import campaigns as campaign_service

        self.enforce()
        campaign = self.scheduled()

        with patch.object(
            entitlement_service, "check", side_effect=RuntimeError("lookup broke")
        ):
            with self.assertLogs("core.services.campaigns", level="ERROR"):
                campaign_service.start_due_campaigns()

        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.MATERIALIZING)
