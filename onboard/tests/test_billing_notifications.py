"""Section 18 of the billing test plan — BN-01 to BN-15.

Notices about money. Two rules run through all of them.

**In-app is the record; push is a nudge.** The in-app row is written first and
synchronously, so the notice exists even if FCM is unreachable or the tenant has no
devices registered. BN-09 and BN-11 are what keep that ordering.

**A notification must never be why something else failed.** Renewals, expiries and
credit grants all fire notices, and none of them may be rolled back or reported as
failed because a push could not be queued. BN-12 is that guarantee.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from core.models.in_app_notification import InAppNotification
from core.models.notification_log import NotificationLog
from onboard.models import Subscription
from onboard.services import billing_notifications
from onboard.services import subscriptions as subscription_service
from onboard.tests.billing_factories import make_plan, make_tenant
from tenant.models import TenantUserMapping
from wallet.models import CreditLedger, Wallet
from wallet.services import wallet as wallet_service

Bucket = CreditLedger.Bucket
EntryType = CreditLedger.EntryType
NoticeType = NotificationLog.NotificationType


class NotificationTestCase(TestCase):
    """A tenant on a plan, with a live subscription to talk about."""

    def setUp(self):
        self.ctx = make_tenant("notifications")
        self.tenant = self.ctx.tenant
        self.plan = make_plan(
            "notifications-plan",
            allocated_credits=1000,
            low_balance_threshold=100,
            cost_per_additional_credit=Decimal("1.00"),
        )
        self.subscription = subscription_service.activate(self.tenant, self.plan)

    def notices(self, tenant=None):
        return InAppNotification.objects.filter(tenant=tenant or self.tenant)

    def add_user(self, name):
        extra = make_tenant(name)
        TenantUserMapping.objects.filter(user=extra.user).update(tenant=self.tenant)
        return extra.user


class NoticeContentTests(NotificationTestCase):
    """BN-01 to BN-08 — what each notice says, and to whom."""

    def test_bn01_a_renewal_notice_reaches_every_user_on_the_tenant(self):
        """BN-01: whoever is watching the account should hear it, not only whoever
        happened to sign up."""
        self.add_user("notifications-second-user")

        sent = billing_notifications.notify_renewal_due(
            self.subscription, days_left=7
        )

        self.assertEqual(sent, 2)
        self.assertEqual(self.notices().count(), 2)

    def test_bn02_a_renewal_notice_is_filed_under_billing(self):
        """BN-02: the type is what decides which inbox and which push channel."""
        billing_notifications.notify_renewal_due(self.subscription, days_left=7)

        self.assertEqual(
            self.notices().first().notification_type, NoticeType.BILLING
        )

    def test_bn03_the_same_reminder_is_not_repeated_within_a_day(self):
        """BN-03: **the tasks that call this run on a schedule** and can fire more
        than once in a day without meaning to say the same thing twice."""
        billing_notifications.notify_renewal_due(self.subscription, days_left=7)

        again = billing_notifications.notify_renewal_due(
            self.subscription, days_left=7
        )

        self.assertEqual(again, 0)
        self.assertEqual(self.notices().count(), 1)

    def test_bn04_an_expiry_notice_says_the_purchased_credits_are_safe(self):
        """BN-04: **the sentence that prevents a support ticket.**

        A tenant whose wallet has just frozen with a balance in it will assume the
        credits were taken unless told otherwise.
        """
        billing_notifications.notify_expired(self.subscription)

        notice = self.notices().get()
        self.assertEqual(notice.notification_type, NoticeType.BILLING)
        self.assertIn("kept", notice.body)
        self.assertIn("renew", notice.body.lower())

    def test_bn05_a_low_balance_notice_is_filed_under_credits(self):
        """BN-05: a different inbox from billing — this is about usage, not money
        owed."""
        wallet = Wallet.objects.get(tenant=self.tenant)

        billing_notifications.notify_low_balance(wallet)

        notice = self.notices().get()
        self.assertEqual(notice.notification_type, NoticeType.CREDITS)
        self.assertIn(str(wallet.balance), notice.title)

    def test_bn06_a_credits_added_notice_states_the_amount(self):
        """BN-06: **the payment and the credits are separated by a webhook.**

        A tenant who paid and saw nothing change opens a support ticket; this is
        what closes that gap.
        """
        billing_notifications.notify_credits_added(
            self.tenant.id, credits=500, reason="Your credit top-up has been applied"
        )

        notice = self.notices().get()
        self.assertEqual(notice.notification_type, NoticeType.CREDITS)
        self.assertIn("500", notice.title)

    def test_bn07_a_plan_change_notice_names_where_they_came_from(self):
        """BN-07: the notice that matters most is the scheduled downgrade firing on
        its own — nobody is watching when a cycle boundary passes at 3am."""
        billing_notifications.notify_plan_changed(
            self.subscription, previous_plan_code="old_plan"
        )

        notice = self.notices().get()
        self.assertIn(self.plan.name, notice.title)
        self.assertIn("old_plan", notice.body)

    def test_bn08_a_tenant_with_nobody_to_tell_is_not_an_error(self):
        """BN-08: this runs inside a webhook, where raising would make Razorpay
        retry a payment we already accepted."""
        TenantUserMapping.all_objects.filter(tenant=self.tenant).delete()

        with self.assertLogs("onboard.services.billing_notifications", level="INFO"):
            sent = billing_notifications.notify_expired(self.subscription)

        self.assertEqual(sent, 0)


class DeliveryOrderingTests(NotificationTestCase):
    """BN-09 to BN-12 — the record survives the nudge failing."""

    def test_bn09_the_in_app_row_exists_before_push_is_attempted(self):
        """BN-09: **in-app is the record; push is a nudge.**

        Asserted by observing the database from inside the push call, because the
        end state cannot tell a correct ordering from a lucky one.
        """
        observed = {}

        def spy(tenant_id, user, title, body, notification_type, route):
            observed["rows_at_push_time"] = InAppNotification.objects.filter(
                tenant_id=tenant_id
            ).count()

        with patch.object(billing_notifications, "_push", side_effect=spy):
            billing_notifications.notify_expired(self.subscription)

        self.assertEqual(observed["rows_at_push_time"], 1)

    def test_bn10_the_push_payload_carries_somewhere_to_go(self):
        """BN-10: a push that opens the dashboard root makes the tenant hunt for
        the thing it was telling them about."""
        captured = {}

        with patch("core.tasks.send_push_to_user_task.delay") as queued:
            billing_notifications.notify_low_balance(
                Wallet.objects.get(tenant=self.tenant)
            )
            captured.update(queued.call_args.kwargs)

        self.assertEqual(captured["data"]["route"], "/billing/wallet")
        self.assertEqual(captured["data"]["type"], NoticeType.CREDITS)

    def test_bn11_a_push_failure_leaves_the_notice_standing(self):
        """BN-11: **a billing reminder that could not be pushed is still a billing
        reminder the tenant will see next time they open the dashboard.**"""
        with patch(
            "core.tasks.send_push_to_user_task.delay",
            side_effect=RuntimeError("FCM unreachable"),
        ):
            with self.assertLogs(
                "onboard.services.billing_notifications", level="WARNING"
            ):
                sent = billing_notifications.notify_expired(self.subscription)

        self.assertEqual(sent, 1)
        self.assertEqual(self.notices().count(), 1)

    def test_bn12_a_notification_failure_never_undoes_a_lifecycle_change(self):
        """BN-12: **the guarantee.**

        A subscription that really did expire must not be reported as failed — or
        worse, rolled back — because a notice could not be written.
        """
        Subscription.objects.filter(pk=self.subscription.pk).update(
            end_date=timezone.now() - timedelta(days=1)
        )
        self.subscription.refresh_from_db()

        with patch.object(
            billing_notifications, "notify_expired",
            side_effect=RuntimeError("notification backend down"),
        ):
            with self.assertLogs(
                "onboard.services.subscriptions", level="WARNING"
            ):
                subscription_service.expire(self.subscription)

        self.subscription.refresh_from_db()
        self.assertEqual(
            self.subscription.status,
            Subscription.SubscriptionStatusChoices.EXPIRED,
        )
        self.assertTrue(Wallet.objects.get(tenant=self.tenant).is_frozen)


class LifecycleWiringTests(NotificationTestCase):
    """BN-13 to BN-15 — the notices actually fire from the right places."""

    def test_bn13_a_replayed_top_up_does_not_announce_the_credits_twice(self):
        """BN-13: **telling a tenant twice that their credits arrived is worse than
        not telling them.**

        The grant returns ``None`` on a replay, and the notice is keyed off that
        rather than off the webhook being received.
        """
        from onboard.models import Transaction
        from onboard.tests.billing_factories import make_transaction, pay

        order = make_transaction(
            self.tenant, self.plan,
            status=Transaction.TransactionStatusChoices.PENDING,
            transaction_type=Transaction.TransactionTypeChoices.TOP_UP,
            credits_purchased=500,
            subscription=self.subscription,
        )
        pay(order.razorpay_order_id, event_id="evt_notice_first")
        first_count = self.notices().filter(
            notification_type=NoticeType.CREDITS
        ).count()

        # A redelivery carrying a fresh event id: past the log, stopped by the grant.
        pay(order.razorpay_order_id, event_id="evt_notice_second")

        self.assertEqual(first_count, 1)
        self.assertEqual(
            self.notices().filter(notification_type=NoticeType.CREDITS).count(), 1
        )

    def test_bn14_expiry_announces_itself(self):
        """BN-14: the tenant's calls have just stopped; being told why is the
        minimum."""
        Subscription.objects.filter(pk=self.subscription.pk).update(
            end_date=timezone.now() - timedelta(days=1)
        )
        self.subscription.refresh_from_db()

        subscription_service.expire(self.subscription)

        self.assertTrue(
            self.notices().filter(
                notification_type=NoticeType.BILLING, title__contains="has ended"
            ).exists()
        )

    def test_bn15_an_applied_plan_change_announces_itself(self):
        """BN-15: fired from ``apply_plan_change``, so it covers both an upgrade
        clearing and a scheduled downgrade firing unattended."""
        bigger = make_plan("notifications-bigger", allocated_credits=3000)

        subscription_service.apply_plan_change(self.subscription, bigger)

        self.assertTrue(
            self.notices().filter(
                notification_type=NoticeType.BILLING,
                title__contains=bigger.name,
            ).exists()
        )
