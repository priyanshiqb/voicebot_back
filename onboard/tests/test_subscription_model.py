"""Section 8 of the billing test plan — SM-01 to SM-18.

``Subscription`` is the contract, not the balance: what the tenant bought and until
when. Two things it must get right are tested here — the calendar arithmetic that
decides when a cycle ends, and the snapshot that freezes the terms so that editing a
plan changes *future* cycles only.
"""

from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from onboard.constants.entitlement_resource import EntitlementResource
from onboard.constants.reset_period import ResetPeriod
from onboard.models import Plan, Subscription
from onboard.tests.billing_factories import make_plan, make_tenant

Status = Subscription.SubscriptionStatusChoices


def aware(year, month, day, hour=10):
    return timezone.make_aware(datetime(year, month, day, hour, 0))


class CycleArithmeticTests(TestCase):
    """SM-01 to SM-05, SM-16, SM-17 — when a cycle ends."""

    def setUp(self):
        self.tenant = make_tenant("cycle").tenant
        self.monthly = make_plan("cycle-monthly")
        self.yearly = make_plan(
            "cycle-yearly", billing_cycle=Plan.BillingCycleChoices.YEARLY
        )

    def subscribe(self, plan, **overrides):
        return Subscription.objects.create(
            tenant=self.tenant, plan=plan, **overrides
        )

    def test_sm01_a_monthly_cycle_ends_one_calendar_month_later(self):
        """SM-01."""
        subscription = self.subscribe(self.monthly, start_date=aware(2026, 3, 15))

        self.assertEqual(subscription.end_date, aware(2026, 4, 15))

    def test_sm02_a_yearly_cycle_ends_one_year_later(self):
        """SM-02."""
        subscription = self.subscribe(self.yearly, start_date=aware(2026, 3, 15))

        self.assertEqual(subscription.end_date, aware(2027, 3, 15))

    def test_sm03_a_cycle_starting_on_the_31st_lands_at_the_end_of_february(self):
        """SM-03: **calendar arithmetic, not 30 days.**

        Thirty days from 31 January is 2 March, which would bill the tenant for two
        days they did not buy and push every subsequent cycle further adrift.
        """
        subscription = self.subscribe(self.monthly, start_date=aware(2026, 1, 31))

        self.assertEqual(subscription.end_date, aware(2026, 2, 28))

    def test_sm04_an_existing_end_date_is_never_recomputed(self):
        """SM-04: **a renewal has already pushed this forward.**

        Recomputing it on the next save would silently undo the cycle the tenant
        just paid for.
        """
        subscription = self.subscribe(self.monthly, start_date=aware(2026, 3, 15))
        renewed_to = aware(2026, 5, 15)
        subscription.end_date = renewed_to
        subscription.save(update_fields=["end_date"])

        subscription.status = Status.ACTIVE
        subscription.save()
        subscription.refresh_from_db()

        self.assertEqual(subscription.end_date, renewed_to)

    def test_sm05_the_database_refuses_a_null_end_date(self):
        """SM-05: expiry, renewal windows and proration are all functions of this.

        ``save()`` fills it when unset, so the column constraint is a backstop
        rather than a burden — but it is the backstop that stopped every row in
        this table being nullable again.
        """
        subscription = self.subscribe(self.monthly)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Subscription.objects.filter(pk=subscription.pk).update(end_date=None)

    def test_sm16_an_unrecognised_cycle_falls_back_to_monthly(self):
        """SM-16: a subscription with no cycle at all is worse than one with a
        conservative guess, because expiry silently never fires for it."""
        subscription = self.subscribe(self.monthly, start_date=aware(2026, 3, 15))
        subscription.plan_snapshot["billing_cycle"] = "fortnightly"

        self.assertEqual(
            subscription.derive_end_date(), aware(2026, 4, 15)
        )

    def test_sm17_is_expired_flips_at_the_end_date(self):
        """SM-17."""
        subscription = self.subscribe(self.monthly)
        self.assertFalse(subscription.is_expired)

        Subscription.objects.filter(pk=subscription.pk).update(
            end_date=timezone.now() - timedelta(seconds=1)
        )
        subscription.refresh_from_db()

        self.assertTrue(subscription.is_expired)


class ActiveUniquenessTests(TestCase):
    """SM-06 to SM-08 — exactly one live subscription per tenant."""

    def setUp(self):
        self.tenant = make_tenant("uniqueness").tenant
        self.plan = make_plan("uniqueness-plan")
        self.other_plan = make_plan("uniqueness-other-plan")

    def subscribe(self, plan=None, **overrides):
        return Subscription.objects.create(
            tenant=self.tenant, plan=plan or self.plan, **overrides
        )

    def test_sm06_a_second_active_subscription_is_refused(self):
        """SM-06: **without this, "the current subscription" is ambiguous** and an
        upgrade that creates a second row lets the wrong one be read."""
        self.subscribe()

        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                self.subscribe(self.other_plan)

        self.assertIn("uniq_active_subscription_per_tenant", str(caught.exception))

    def test_sm07_a_cancelled_subscription_does_not_block_a_new_one(self):
        """SM-07: this is what every upgrade looks like — close one, open another."""
        old = self.subscribe()
        old.status = Status.CANCELLED
        old.canceled_at = timezone.now()
        old.save(update_fields=["status", "canceled_at"])

        new = self.subscribe(self.other_plan)

        self.assertEqual(
            Subscription.objects.filter(
                tenant=self.tenant, status=Status.ACTIVE
            ).count(),
            1,
        )
        self.assertNotEqual(new.pk, old.pk)

    def test_sm08_a_soft_deleted_active_row_does_not_block_a_replacement(self):
        """SM-08: the constraint is partial on ``is_deleted``."""
        old = self.subscribe()
        old.soft_delete()

        new = self.subscribe(self.other_plan)

        self.assertNotEqual(new.pk, old.pk)
        self.assertEqual(Subscription.all_objects.filter(tenant=self.tenant).count(), 2)


class PlanSnapshotTests(TestCase):
    """SM-09 to SM-15, SM-18 — the frozen contract."""

    CREDIT_RULE_KEYS = (
        "allocated_credits",
        "cost_per_additional_credit",
        "max_rollover_credits",
        "allow_rollover_on_renewal",
        "allow_rollover_on_upgrade",
        "allow_rollover_on_downgrade",
        "allow_use_after_expiry",
        "low_balance_threshold",
        "renews_without_payment",
    )

    def setUp(self):
        self.tenant = make_tenant("snapshot-model").tenant
        self.plan = make_plan(
            "snapshot-model-plan",
            allocated_credits=1500,
            max_rollover_credits=400,
            allow_rollover_on_renewal=True,
        )
        self.subscription = Subscription.objects.create(
            tenant=self.tenant, plan=self.plan
        )

    def test_sm09_the_snapshot_is_written_on_first_save(self):
        """SM-09: money, credit rules and allowances, all frozen together."""
        snapshot = self.subscription.plan_snapshot

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["code"], self.plan.code)
        self.assertEqual(snapshot["net_base_price"], str(self.plan.net_base_price))
        self.assertEqual(snapshot["allocated_credits"], 1500)
        self.assertIn("entitlements", snapshot)

    def test_sm10_every_credit_rule_is_frozen(self):
        """SM-10: **without these, an admin toggling a rollover flag silently
        rewrites the terms of every live subscription.**"""
        snapshot = self.subscription.plan_snapshot

        for key in self.CREDIT_RULE_KEYS:
            self.assertIn(key, snapshot, f"{key} is not frozen onto the subscription")

    def test_sm11_allowances_are_frozen_per_resource_with_their_window(self):
        """SM-11: the entitlement checker reads this, so a plan edit must not
        retroactively shrink a limit a tenant is already working inside.

        **The reset period is frozen beside the number**, because the number means
        nothing without it — ten a month and ten a year are the same figure. If the
        period lived only in code, changing a plan's window would silently
        re-interpret every subscription already sold.
        """
        entitlements = self.subscription.plan_snapshot["entitlements"]

        self.assertEqual(
            set(entitlements),
            {row.resource for row in self.plan.entitlements.all()},
        )
        agent = entitlements[EntitlementResource.AGENT]
        self.assertEqual(agent["included"], 2)
        self.assertEqual(agent["reset_period"], ResetPeriod.NONE)

        campaigns = entitlements[EntitlementResource.CAMPAIGN_MONTHLY]
        self.assertEqual(campaigns["reset_period"], ResetPeriod.MONTHLY)

    def test_sm12_rules_ignore_later_edits_to_the_live_plan(self):
        """SM-12: **the freeze guarantee, stated as a test.**"""
        self.plan.allocated_credits = 99999
        self.plan.allow_rollover_on_renewal = False
        self.plan.save(update_fields=["allocated_credits", "allow_rollover_on_renewal"])

        rules = self.subscription.rules

        self.assertEqual(rules["allocated_credits"], 1500)
        self.assertTrue(rules["allow_rollover_on_renewal"])

    def test_sm13_a_key_the_snapshot_predates_falls_back_to_the_plan(self):
        """SM-13: subscriptions written before a rule existed must keep working,
        rather than raising a KeyError on a hot path."""
        del self.subscription.plan_snapshot["max_rollover_credits"]

        self.assertEqual(
            self.subscription.rules["max_rollover_credits"],
            self.plan.max_rollover_credits,
        )

    def test_sm14_included_entitlement_reads_the_snapshot(self):
        """SM-14."""
        self.plan.entitlements.filter(
            resource=EntitlementResource.AGENT
        ).update(included_quantity=999)

        self.assertEqual(
            self.subscription.included_entitlement(EntitlementResource.AGENT), 2
        )

    def test_sm15_an_unknown_resource_has_no_allowance(self):
        """SM-15: ``None`` rather than 0 — 'not configured' and 'zero allowed' are
        different answers, and only one of them should block anything."""
        self.assertIsNone(
            self.subscription.included_entitlement("teleportation")
        )

    def test_sm18_has_scheduled_change_tracks_the_scheduled_plan(self):
        """SM-18: a downgrade is stored, never applied early."""
        self.assertFalse(self.subscription.has_scheduled_change)

        cheaper = make_plan("snapshot-model-cheaper")
        self.subscription.scheduled_plan = cheaper
        self.subscription.scheduled_change_at = self.subscription.end_date
        self.subscription.save(
            update_fields=["scheduled_plan", "scheduled_change_at"]
        )

        self.assertTrue(self.subscription.has_scheduled_change)
        self.assertEqual(self.subscription.plan_id, self.plan.pk)
