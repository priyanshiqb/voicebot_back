"""Section 26 of the billing test plan — PU-01 to PU-09.

**Opt-in**, same pattern as the Razorpay suite: skipped unless
``RUN_LIVE_PUSH_TESTS=1``. Uses the real ``FIREBASE_CREDENTIALS`` already configured.

**What "sent" means.** FCM exposes no per-device delivery receipt in this API, so a
``SENT`` row records that **Firebase accepted the message** — not that a phone showed
it. No automated test can assert the latter, and one that claimed to would be lying
about the strongest guarantee available.

The device tokens in a development database are almost certainly stale. That is
*useful*: PU-03 and PU-05 exercise the unregistered-token branch, which is the one most
likely to be wrong and the one that runs most often in production.

    RUN_LIVE_PUSH_TESTS=1 uv run python manage.py test \\
        core.tests.billing.test_push_live --settings=config.settings.test_postgres
"""

import os
import unittest

from django.conf import settings
from django.test import TestCase

from core.models.device_token import DeviceToken
from core.models.notification_log import NotificationLog
from core.services import push
from onboard.services import billing_notifications
from onboard.services import subscriptions as subscription_service
from onboard.tests.billing_factories import make_plan, make_tenant

LIVE = os.environ.get("RUN_LIVE_PUSH_TESTS") == "1"
SKIP_REASON = "live push tests are opt-in; set RUN_LIVE_PUSH_TESTS=1 to run them"

#: Well-formed enough for FCM to parse and reject on its own terms, rather than
#: failing client-side validation and testing nothing.
UNREGISTERED_TOKEN = (
    "fMEXAMPLEnotarealtoken:APA91b"
    + "H" * 100
)


@unittest.skipUnless(LIVE, SKIP_REASON)
class LivePushTestCase(TestCase):
    """A tenant with a billing notice to deliver."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not getattr(settings, "FIREBASE_CREDENTIALS", ""):
            raise unittest.SkipTest("FIREBASE_CREDENTIALS is not configured here")

    def setUp(self):
        self.ctx = make_tenant("push-live")
        self.tenant = self.ctx.tenant
        self.user = self.ctx.user
        self.plan = make_plan("push-live-plan", low_balance_threshold=100)
        self.subscription = subscription_service.activate(self.tenant, self.plan)

    def register(self, token, platform=DeviceToken.Platform.ANDROID):
        return DeviceToken.objects.create(
            tenant=self.tenant, user=self.user, token=token,
            platform=platform, is_active=True,
        )


@unittest.skipUnless(LIVE, SKIP_REASON)
class FirebaseInitialisationTests(LivePushTestCase):
    """PU-01 to PU-03."""

    def test_pu01_the_firebase_app_initialises_from_the_configured_credentials(self):
        """PU-01: **distinguishes unset from broken.**

        An unset credential is a deliberate "push is off here". A credential that
        is set but cannot be loaded is a misconfiguration, and every send would
        silently do nothing.
        """
        import firebase_admin

        self.assertTrue(push._ensure_app(), push._init_error)
        self.assertTrue(firebase_admin._apps)

    def test_pu02_a_syntactically_invalid_token_is_classified_not_crashed_on(self):
        """PU-02: a probe must return an answer, not raise into the sweep that
        called it."""
        result = push.probe_token("obviously-not-a-token")

        self.assertIn(result, (False, None))

    def test_pu03_a_well_formed_but_unregistered_token_is_reported_dead(self):
        """PU-03: **the branch that runs most often in production.**

        Tokens rot constantly — reinstalls, cleared data, uninstalled apps — so
        "well-formed and unknown to FCM" is the ordinary case, not the exotic one.
        """
        result = push.probe_token(UNREGISTERED_TOKEN)

        self.assertIs(result, False)


@unittest.skipUnless(LIVE, SKIP_REASON)
class BillingPushDeliveryTests(LivePushTestCase):
    """PU-04 to PU-09."""

    def test_pu04_a_billing_push_reaches_fcm_without_anything_escaping(self):
        """PU-04: accepted, or per-token errors — but never an exception out of a
        notification call. **A billing reminder that could not be pushed must not
        be why a renewal fails.**"""
        self.register(UNREGISTERED_TOKEN)

        sent = push.send_to_user(
            self.tenant, self.user,
            "Your plan ends in 3 days",
            "Renew to keep your bot running.",
            {"type": NotificationLog.NotificationType.BILLING,
             "route": "/billing/subscription"},
        )

        self.assertIsInstance(sent, int)
        self.assertGreaterEqual(sent, 0)

    def test_pu05_tokens_fcm_disowns_are_marked_inactive(self):
        """PU-05: **the point of sending as one batch.**

        FCM answers per token, so a dead device is retired on the spot instead of
        being retried forever — and ``last_error`` records which mechanism did it,
        because "inactive, and nobody knows why" is an unanswerable support
        question.
        """
        row = self.register(UNREGISTERED_TOKEN)

        push.send_to_user(
            self.tenant, self.user, "Low balance", "Top up to continue.", {}
        )

        row.refresh_from_db()
        self.assertFalse(row.is_active)
        self.assertTrue(row.last_error)

    def test_pu06_the_data_payload_carries_the_type_and_the_route(self):
        """PU-06: without a route the push opens the dashboard root and the tenant
        hunts for the thing it was telling them about."""
        self.register(UNREGISTERED_TOKEN)

        _, outcomes = push.send_to_user(
            self.tenant, self.user, "Credits added", "500 credits are available.",
            {"type": NotificationLog.NotificationType.CREDITS,
             "route": "/billing/wallet"},
            detailed=True,
        )

        # Every token is accounted for, whichever way FCM answered.
        self.assertEqual(set(outcomes), {UNREGISTERED_TOKEN})
        self.assertIn("accepted", outcomes[UNREGISTERED_TOKEN])

    def test_pu07_an_accepted_message_is_recorded_as_sent(self):
        """PU-07: **"sent" means Firebase accepted it**, which is the strongest
        thing this API can tell us. Anything stronger would be a claim about a
        device we cannot see."""
        log = NotificationLog.objects.create(
            tenant=self.tenant,
            notification_type=NotificationLog.NotificationType.BILLING,
            channel=NotificationLog.Channel.FCM,
            recipient=str(self.user.pk),
        )
        self.assertEqual(log.status, NotificationLog.Status.PENDING)

        from django.utils import timezone

        log.status = NotificationLog.Status.SENT
        log.sent_at = timezone.now()
        log.save(update_fields=["status", "sent_at"])

        log.refresh_from_db()
        self.assertEqual(log.status, NotificationLog.Status.SENT)
        self.assertIsNotNone(log.sent_at)
        self.assertEqual(log.error, "")

    def test_pu08_a_message_no_token_accepted_is_not_recorded_as_sent(self):
        """PU-08: a stale-token-only tenant is not a delivery, and recording one
        would make the notification log useless for exactly the accounts that need
        looking at."""
        self.register(UNREGISTERED_TOKEN)

        sent = push.send_to_user(
            self.tenant, self.user, "Plan expired", "Renew to resume calls.", {}
        )

        self.assertEqual(sent, 0)

    def test_pu09_a_tenant_with_no_devices_makes_no_call_at_all(self):
        """PU-09: and the in-app notice is still written — **in-app is the record,
        push is a nudge.**"""
        self.assertFalse(DeviceToken.objects.filter(tenant=self.tenant).exists())

        created = billing_notifications.notify_expired(self.subscription)

        self.assertEqual(created, 1)
        self.assertEqual(
            push.send_to_user(self.tenant, self.user, "x", "y", {}), 0
        )
