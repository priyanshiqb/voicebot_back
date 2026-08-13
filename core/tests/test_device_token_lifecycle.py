"""
Device-token liveness, retention, and the send path's failure attribution.

Everything here guards a behaviour that did not exist before: nothing deleted a
device token, nothing aged one out, and the send path could not tell "this device
is gone" from "FCM was busy" from "we built the message wrong".

The tests that matter most are the ones asserting what is *not* done — a transient
failure that leaves a token alone, an active row the purge refuses to delete, a
whole-batch INVALID_ARGUMENT that strikes nothing. Retiring too eagerly is silent:
the device simply stops receiving alerts and nobody finds out.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from rest_framework.throttling import ScopedRateThrottle

from core.models.device_token import DeviceToken
from core.services import device_tokens as token_service
from core.services import push
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models import CustomUser as User


# --- FCM doubles --------------------------------------------------------------
#
# firebase-admin classifies by exception type and `push._classify` matches on the
# class *name*, so these stand in for the real symbols without importing the SDK.


class UnregisteredError(Exception):
    pass


class SenderIdMismatchError(Exception):
    pass


class InvalidArgumentError(Exception):
    pass


class QuotaExceededError(Exception):
    pass


class ThirdPartyAuthError(Exception):
    pass


class FakeResult:
    def __init__(self, success=True, exception=None):
        self.success = success
        self.exception = exception


class FakeBatch:
    def __init__(self, results):
        self.responses = results


def batch_of(*results):
    return FakeBatch(list(results))


class DeviceTokenFixtureMixin:
    def make_tenant_user(self, slug="dt"):
        tenant = Tenant.objects.create(name=f"T {slug}", subdomain=f"tenant-{slug}")
        user = User.objects.create_user(
            username=f"user-{slug}", email=f"{slug}@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=user, tenant=tenant)
        return tenant, user

    def make_token(self, tenant, user, token, *, last_seen_days_ago=0, **kwargs):
        row = DeviceToken.objects.create(
            tenant=tenant, user=user, token=token, **kwargs
        )
        if last_seen_days_ago is not None:
            DeviceToken.objects.filter(pk=row.pk).update(
                last_seen_at=timezone.now() - timedelta(days=last_seen_days_ago)
            )
            row.refresh_from_db()
        return row


@override_settings(DEVICE_TOKEN_STALE_DAYS=60, DEVICE_TOKEN_RETENTION_DAYS=90)
class StalenessTests(DeviceTokenFixtureMixin, TestCase):
    def setUp(self):
        self.tenant, self.user = self.make_tenant_user("stale")

    def test_a_recently_registered_token_is_deliverable(self):
        self.make_token(self.tenant, self.user, "fresh", last_seen_days_ago=1)
        self.assertEqual(token_service.deliverable().count(), 1)

    def test_a_token_past_the_staleness_window_is_not_deliverable(self):
        self.make_token(self.tenant, self.user, "old", last_seen_days_ago=61)
        self.assertEqual(token_service.deliverable().count(), 0)
        self.assertEqual(token_service.stale_queryset().count(), 1)

    def test_a_null_last_seen_falls_back_to_created_at(self):
        """The bug this exists for: `last_seen_at__lt=cutoff` never matches NULL.

        Every row predating the staleness feature has a NULL `last_seen_at`, as does
        anything created through the admin or a fixture. Filtering on the column
        directly would have made exactly those rows immortal — never stale, never
        purged — which is the population most in need of pruning.
        """
        row = self.make_token(self.tenant, self.user, "nulled", last_seen_days_ago=None)
        DeviceToken.objects.filter(pk=row.pk).update(
            last_seen_at=None, created_at=timezone.now() - timedelta(days=200)
        )

        self.assertEqual(token_service.deliverable().count(), 0)
        self.assertEqual(token_service.stale_queryset().count(), 1)

    def test_a_null_last_seen_on_a_new_row_is_still_deliverable(self):
        row = self.make_token(self.tenant, self.user, "newish", last_seen_days_ago=None)
        DeviceToken.objects.filter(pk=row.pk).update(last_seen_at=None)
        self.assertEqual(token_service.deliverable().count(), 1)

    def test_deactivate_stale_records_why(self):
        self.make_token(self.tenant, self.user, "old", last_seen_days_ago=90)
        self.assertEqual(token_service.deactivate_stale(), 1)

        row = DeviceToken.objects.get(token="old")
        self.assertFalse(row.is_active)
        self.assertIn("60 days", row.last_error)

    def test_deactivate_stale_dry_run_changes_nothing(self):
        self.make_token(self.tenant, self.user, "old", last_seen_days_ago=90)
        self.assertEqual(token_service.deactivate_stale(dry_run=True), 1)
        self.assertTrue(DeviceToken.objects.get(token="old").is_active)

    def test_the_send_path_skips_stale_tokens(self):
        self.make_token(self.tenant, self.user, "fresh", last_seen_days_ago=1)
        self.make_token(self.tenant, self.user, "old", last_seen_days_ago=61)

        with patch.object(push, "_ensure_app", return_value=True), patch(
            "firebase_admin.messaging.send_each_for_multicast"
        ) as send:
            send.return_value = batch_of(FakeResult())
            push.send_to_user(self.tenant, self.user, "t", "b")

        tokens = send.call_args[0][0].tokens
        self.assertEqual(tokens, ["fresh"])


@override_settings(DEVICE_TOKEN_STALE_DAYS=60, DEVICE_TOKEN_RETENTION_DAYS=90)
class RetentionTests(DeviceTokenFixtureMixin, TestCase):
    def setUp(self):
        self.tenant, self.user = self.make_tenant_user("purge")

    def test_an_inactive_row_beyond_retention_is_deleted(self):
        self.make_token(
            self.tenant, self.user, "dead", last_seen_days_ago=100, is_active=False
        )
        self.assertEqual(token_service.purge(), 1)
        self.assertEqual(DeviceToken.all_objects.count(), 0)

    def test_an_inactive_row_inside_retention_is_kept(self):
        self.make_token(
            self.tenant, self.user, "recent", last_seen_days_ago=10, is_active=False
        )
        self.assertEqual(token_service.purge(), 0)
        self.assertEqual(DeviceToken.all_objects.count(), 1)

    def test_an_active_row_is_never_purged_however_old(self):
        """Demote before delete.

        An active row is skipped no matter its age, so a token is always visible as
        inactive for a full retention window before it disappears. Purging straight
        from active would delete a device the day the staleness window passed, and
        "my phone stopped getting alerts" would have no row left to explain it.
        """
        self.make_token(self.tenant, self.user, "ancient", last_seen_days_ago=900)
        self.assertEqual(token_service.purge(), 0)
        self.assertEqual(DeviceToken.all_objects.count(), 1)

    def test_a_soft_deleted_row_beyond_retention_is_hard_deleted(self):
        row = self.make_token(self.tenant, self.user, "gone", last_seen_days_ago=100)
        DeviceToken.objects.filter(pk=row.pk).update(is_deleted=True)

        self.assertEqual(token_service.purge(), 1)
        self.assertEqual(DeviceToken.all_objects.count(), 0)

    def test_dry_run_counts_without_deleting(self):
        self.make_token(
            self.tenant, self.user, "dead", last_seen_days_ago=100, is_active=False
        )
        self.assertEqual(token_service.purge(dry_run=True), 1)
        self.assertEqual(DeviceToken.all_objects.count(), 1)

    def test_purge_batches(self):
        for index in range(5):
            self.make_token(
                self.tenant,
                self.user,
                f"dead-{index}",
                last_seen_days_ago=100,
                is_active=False,
            )
        self.assertEqual(token_service.purge(batch=2), 5)
        self.assertEqual(DeviceToken.all_objects.count(), 0)

    def test_sweep_demotes_then_purges_in_one_run(self):
        self.make_token(self.tenant, self.user, "stale", last_seen_days_ago=70)
        self.make_token(
            self.tenant, self.user, "dead", last_seen_days_ago=100, is_active=False
        )

        result = token_service.sweep()
        self.assertEqual(result["deactivated"], 1)
        self.assertEqual(result["purged"], 1)
        self.assertFalse(DeviceToken.objects.get(token="stale").is_active)


class UserCapTests(DeviceTokenFixtureMixin, TestCase):
    def setUp(self):
        self.tenant, self.user = self.make_tenant_user("cap")

    @override_settings(DEVICE_TOKEN_MAX_PER_USER=3)
    def test_the_oldest_registrations_are_retired_first(self):
        for index in range(5):
            self.make_token(
                self.tenant, self.user, f"tok-{index}", last_seen_days_ago=5 - index
            )

        self.assertEqual(token_service.enforce_user_cap(self.tenant, self.user), 2)

        live = set(
            DeviceToken.objects.filter(is_active=True).values_list("token", flat=True)
        )
        self.assertEqual(live, {"tok-2", "tok-3", "tok-4"})

    @override_settings(DEVICE_TOKEN_MAX_PER_USER=2)
    def test_a_null_last_seen_row_does_not_outrank_current_devices(self):
        """Postgres sorts NULLs first under DESC.

        Ordering on `-last_seen_at` directly would rank a never-re-registered row as
        the newest device and retire the ones actually in use. The cap orders on the
        coalesced value instead.
        """
        legacy = self.make_token(self.tenant, self.user, "legacy", last_seen_days_ago=None)
        DeviceToken.objects.filter(pk=legacy.pk).update(
            last_seen_at=None, created_at=timezone.now() - timedelta(days=300)
        )
        self.make_token(self.tenant, self.user, "current-a", last_seen_days_ago=0)
        self.make_token(self.tenant, self.user, "current-b", last_seen_days_ago=1)

        token_service.enforce_user_cap(self.tenant, self.user)

        self.assertFalse(DeviceToken.objects.get(token="legacy").is_active)
        self.assertTrue(DeviceToken.objects.get(token="current-a").is_active)

    @override_settings(DEVICE_TOKEN_MAX_PER_USER=5)
    def test_under_the_cap_nothing_is_touched(self):
        self.make_token(self.tenant, self.user, "only")
        self.assertEqual(token_service.enforce_user_cap(self.tenant, self.user), 0)
        self.assertTrue(DeviceToken.objects.get(token="only").is_active)

    @override_settings(DEVICE_TOKEN_MAX_PER_USER=2)
    def test_registration_enforces_the_cap_and_keeps_the_new_device(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        self.make_token(self.tenant, self.user, "old-a", last_seen_days_ago=9)
        self.make_token(self.tenant, self.user, "old-b", last_seen_days_ago=8)

        response = client.post(
            "/api/v1/core/device-tokens/", {"token": "brand-new"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(DeviceToken.objects.get(token="brand-new").is_active)
        self.assertEqual(DeviceToken.objects.filter(is_active=True).count(), 2)


class FailureStrikeTests(DeviceTokenFixtureMixin, TestCase):
    def setUp(self):
        self.tenant, self.user = self.make_tenant_user("strike")
        self.row = self.make_token(self.tenant, self.user, "tok")

    @override_settings(DEVICE_TOKEN_FAILURE_STRIKES=3)
    def test_an_ambiguous_failure_strikes_without_retiring(self):
        token_service.record_ambiguous_failure("tok", "INVALID_ARGUMENT: nope")
        self.row.refresh_from_db()
        self.assertEqual(self.row.failure_count, 1)
        self.assertTrue(self.row.is_active)

    @override_settings(DEVICE_TOKEN_FAILURE_STRIKES=3)
    def test_the_token_is_retired_at_the_limit(self):
        for _ in range(3):
            token_service.record_ambiguous_failure("tok", "INVALID_ARGUMENT: nope")
        self.row.refresh_from_db()
        self.assertEqual(self.row.failure_count, 3)
        self.assertFalse(self.row.is_active)
        self.assertIn("x3", self.row.last_error)

    @override_settings(DEVICE_TOKEN_FAILURE_STRIKES=3)
    def test_a_success_resets_the_counter(self):
        token_service.record_ambiguous_failure("tok", "INVALID_ARGUMENT: nope")
        token_service.record_ambiguous_failure("tok", "INVALID_ARGUMENT: nope")
        token_service.record_success("tok")

        self.row.refresh_from_db()
        self.assertEqual(self.row.failure_count, 0)
        self.assertEqual(self.row.last_error, "")

    def test_re_registering_clears_the_strike_count(self):
        DeviceToken.objects.filter(pk=self.row.pk).update(
            failure_count=2, last_error="INVALID_ARGUMENT: nope", is_active=False
        )
        client = APIClient()
        client.force_authenticate(user=self.user)

        client.post("/api/v1/core/device-tokens/", {"token": "tok"}, format="json")

        self.row.refresh_from_db()
        self.assertEqual(self.row.failure_count, 0)
        self.assertEqual(self.row.last_error, "")
        self.assertTrue(self.row.is_active)

    def test_retire_records_the_reason(self):
        token_service.retire("tok", "FCM rejected the token: UnregisteredError")
        self.row.refresh_from_db()
        self.assertFalse(self.row.is_active)
        self.assertIn("UnregisteredError", self.row.last_error)


class SendClassificationTests(DeviceTokenFixtureMixin, TestCase):
    def setUp(self):
        self.tenant, self.user = self.make_tenant_user("send")

    def send(self, tokens, results, **kwargs):
        for token in tokens:
            if not DeviceToken.objects.filter(token=token).exists():
                self.make_token(self.tenant, self.user, token)
        with patch.object(push, "_ensure_app", return_value=True), patch(
            "firebase_admin.messaging.send_each_for_multicast"
        ) as call:
            call.return_value = batch_of(*results)
            sent = push._send(tokens, "t", "b", **kwargs)
        return sent, call

    def test_an_accepted_send_is_counted(self):
        sent, _ = self.send(["a"], [FakeResult()])
        self.assertEqual(sent, 1)

    def test_unregistered_retires_the_token_immediately(self):
        self.send(["a"], [FakeResult(False, UnregisteredError("gone"))])
        row = DeviceToken.objects.get(token="a")
        self.assertFalse(row.is_active)
        self.assertIn("UnregisteredError", row.last_error)

    def test_sender_id_mismatch_retires_the_token(self):
        self.send(["a"], [FakeResult(False, SenderIdMismatchError("wrong project"))])
        self.assertFalse(DeviceToken.objects.get(token="a").is_active)

    def test_a_transient_failure_leaves_the_token_completely_alone(self):
        """Quota and outage errors say nothing about whether a device exists.

        Counting them would retire healthy devices during an FCM incident — the
        moment the fan-out matters most and the moment it is least observable.
        """
        self.send(["a"], [FakeResult(False, QuotaExceededError("slow down"))])
        row = DeviceToken.objects.get(token="a")
        self.assertTrue(row.is_active)
        self.assertEqual(row.failure_count, 0)

    def test_an_apns_credential_error_is_not_the_devices_fault(self):
        self.send(["a"], [FakeResult(False, ThirdPartyAuthError("bad apns key"))])
        row = DeviceToken.objects.get(token="a")
        self.assertTrue(row.is_active)
        self.assertEqual(row.failure_count, 0)

    @override_settings(DEVICE_TOKEN_FAILURE_STRIKES=3)
    def test_a_lone_invalid_argument_strikes_that_token(self):
        self.send(
            ["a", "b"],
            [FakeResult(False, InvalidArgumentError("bad token")), FakeResult()],
        )
        self.assertEqual(DeviceToken.objects.get(token="a").failure_count, 1)
        self.assertEqual(DeviceToken.objects.get(token="b").failure_count, 0)

    @override_settings(DEVICE_TOKEN_FAILURE_STRIKES=3)
    def test_a_whole_batch_of_invalid_argument_blames_the_message(self):
        """The discrimination multicast buys us.

        INVALID_ARGUMENT means either a malformed token or a malformed message, and
        FCM does not say which. When every token in a batch fails the same way the
        common factor is the message — so nothing is struck. The per-token loop this
        replaced could not see the pattern, which is why the old code had to choose
        between never retiring and retiring everything.
        """
        self.send(
            ["a", "b", "c"],
            [
                FakeResult(False, InvalidArgumentError("bad message")),
                FakeResult(False, InvalidArgumentError("bad message")),
                FakeResult(False, InvalidArgumentError("bad message")),
            ],
        )
        for token in ("a", "b", "c"):
            row = DeviceToken.objects.get(token=token)
            self.assertEqual(row.failure_count, 0, token)
            self.assertTrue(row.is_active, token)

    def test_a_failed_call_penalises_nothing(self):
        self.make_token(self.tenant, self.user, "a")
        with patch.object(push, "_ensure_app", return_value=True), patch(
            "firebase_admin.messaging.send_each_for_multicast",
            side_effect=RuntimeError("network down"),
        ):
            self.assertEqual(push._send(["a"], "t", "b"), 0)

        row = DeviceToken.objects.get(token="a")
        self.assertTrue(row.is_active)
        self.assertEqual(row.failure_count, 0)

    def test_tokens_are_chunked_at_the_multicast_limit(self):
        tokens = [f"tok-{index}" for index in range(push.FCM_MULTICAST_LIMIT + 10)]
        with patch.object(push, "_ensure_app", return_value=True), patch(
            "firebase_admin.messaging.send_each_for_multicast"
        ) as call:
            call.side_effect = lambda message: batch_of(
                *[FakeResult() for _ in message.tokens]
            )
            sent = push._send(tokens, "t", "b")

        self.assertEqual(call.call_count, 2)
        self.assertEqual(len(call.call_args_list[0][0][0].tokens), push.FCM_MULTICAST_LIMIT)
        self.assertEqual(len(call.call_args_list[1][0][0].tokens), 10)
        self.assertEqual(sent, len(tokens))

    def test_nothing_is_sent_when_push_is_unconfigured(self):
        self.make_token(self.tenant, self.user, "a")
        with override_settings(FIREBASE_CREDENTIALS=""):
            push._app_ready = False
            self.assertEqual(push._send(["a"], "t", "b"), 0)


class MessageEnvelopeTests(TestCase):
    def test_the_collapse_id_is_legal_on_every_platform(self):
        """Web Push caps `Topic` at 32 URL-safe base64 characters.

        A natural key like `booking:<uuid>` is 44 characters with dashes and colons,
        which Web Push rejects outright and APNs silently truncates — so the same
        logical alert would collapse on one platform and not another.
        """
        collapse = push._collapse_id("booking:9f8c1e2a-0000-4a3b-8c1d-2e5f6a7b8c9d")
        self.assertLessEqual(len(collapse), 32)
        self.assertRegex(collapse, r"^[A-Za-z0-9_-]+$")

    def test_the_same_key_always_collapses_the_same_way(self):
        self.assertEqual(push._collapse_id("booking:1"), push._collapse_id("booking:1"))
        self.assertNotEqual(push._collapse_id("booking:1"), push._collapse_id("booking:2"))

    def test_no_collapse_key_means_no_collapse_id(self):
        self.assertIsNone(push._collapse_id(None))
        self.assertIsNone(push._collapse_id(""))

    def test_the_route_is_carried_in_data(self):
        self.assertEqual(
            push._normalise_data({"a": 1}, route="bookings/7"),
            {"a": "1", "route": "bookings/7"},
        )

    def test_an_oversized_payload_is_dropped_but_the_alert_still_goes(self):
        """Truncating would hand the client corrupt JSON; refusing would lose the alert.

        The human still sees the title and body — they just land on the dashboard
        instead of the deep link.
        """
        payload = push._normalise_data({"blob": "x" * (push.MAX_DATA_BYTES + 100)})
        self.assertEqual(payload, {})

    def test_values_are_stringified(self):
        self.assertEqual(push._normalise_data({"n": 5, "b": True}), {"n": "5", "b": "True"})

    @override_settings(FRONTEND_PROJECT_URL="https://app.example.com")
    def test_the_web_click_target_is_absolute(self):
        self.assertEqual(
            push._webpush_link("bookings/7"), "https://app.example.com/bookings/7"
        )

    @override_settings(FRONTEND_PROJECT_URL="http://localhost:3000")
    def test_a_non_https_frontend_yields_no_link(self):
        """FCM rejects a non-HTTPS `link`, so this is inert in development.

        Inert rather than failing: a developer running the stack over http should
        still get the notification, just without the click-through.
        """
        self.assertIsNone(push._webpush_link("bookings/7"))

    def test_no_route_means_no_link(self):
        self.assertIsNone(push._webpush_link(None))

    def test_the_envelope_carries_expiry_and_priority(self):
        import firebase_admin.messaging as messaging

        android, apns, webpush = push._platform_configs(
            messaging, 3600, "abc", "high", False, "bookings/7"
        )
        self.assertEqual(android.ttl, timedelta(seconds=3600))
        self.assertEqual(android.collapse_key, "abc")
        self.assertEqual(apns.headers["apns-priority"], "10")
        self.assertEqual(apns.headers["apns-collapse-id"], "abc")
        self.assertEqual(webpush.headers["TTL"], "3600")
        self.assertEqual(webpush.headers["Topic"], "abc")

    def test_a_data_only_message_wakes_ios(self):
        """Without `content-available`, iOS simply does not deliver a data-only push."""
        import firebase_admin.messaging as messaging

        _, apns, _ = push._platform_configs(messaging, 60, None, "high", True, None)
        self.assertTrue(apns.payload.aps.content_available)

    def test_an_alert_message_does_not_set_content_available(self):
        import firebase_admin.messaging as messaging

        _, apns, _ = push._platform_configs(messaging, 60, None, "high", False, None)
        self.assertIsNone(apns.payload.aps.content_available)


class ProbeTests(DeviceTokenFixtureMixin, TestCase):
    def setUp(self):
        self.tenant, self.user = self.make_tenant_user("probe")

    def test_a_validated_token_is_alive(self):
        with patch.object(push, "_ensure_app", return_value=True), patch(
            "firebase_admin.messaging.send"
        ) as send:
            self.assertIs(push.probe_token("tok"), True)
        self.assertTrue(send.call_args.kwargs.get("dry_run"))

    def test_a_rejected_token_is_dead(self):
        with patch.object(push, "_ensure_app", return_value=True), patch(
            "firebase_admin.messaging.send", side_effect=UnregisteredError("gone")
        ):
            self.assertIs(push.probe_token("tok"), False)

    def test_an_outage_is_inconclusive_not_dead(self):
        with patch.object(push, "_ensure_app", return_value=True), patch(
            "firebase_admin.messaging.send", side_effect=QuotaExceededError("busy")
        ):
            self.assertIsNone(push.probe_token("tok"))

    def test_unconfigured_push_is_inconclusive(self):
        with patch.object(push, "_ensure_app", return_value=False):
            self.assertIsNone(push.probe_token("tok"))

    @override_settings(DEVICE_TOKEN_STALE_DAYS=60)
    def test_probing_retires_only_the_tokens_fcm_rejects(self):
        self.make_token(self.tenant, self.user, "dead", last_seen_days_ago=70)
        self.make_token(self.tenant, self.user, "alive", last_seen_days_ago=70)

        with patch.object(push, "probe_token", side_effect=lambda t: t != "dead"):
            tally = token_service.probe_stale()

        self.assertEqual(tally["dead"], 1)
        self.assertEqual(tally["alive"], 1)
        self.assertFalse(DeviceToken.objects.get(token="dead").is_active)
        self.assertTrue(DeviceToken.objects.get(token="alive").is_active)

    @override_settings(DEVICE_TOKEN_STALE_DAYS=60)
    def test_an_inconclusive_probe_retires_nothing(self):
        self.make_token(self.tenant, self.user, "unknown", last_seen_days_ago=70)
        with patch.object(push, "probe_token", return_value=None):
            tally = token_service.probe_stale()

        self.assertEqual(tally["unknown"], 1)
        self.assertTrue(DeviceToken.objects.get(token="unknown").is_active)


class LogoutDeviceTests(DeviceTokenFixtureMixin, APITestCase):
    def setUp(self):
        self.tenant, self.user = self.make_tenant_user("logout")
        self.other = User.objects.create_user(
            username="other-logout", email="other-logout@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=self.other, tenant=self.tenant)
        self.make_token(self.tenant, self.user, "mine")

    def login(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken

        from user.services import sessions as session_service

        refresh = RefreshToken.for_user(user)
        access = refresh.access_token
        request = type("R", (), {"headers": {}, "META": {}})()
        session_service.create_for_tokens(user, request, refresh, access)
        return str(refresh)

    def test_logout_deactivates_the_device_that_signed_out(self):
        refresh = self.login(self.user)
        self.client.force_authenticate(user=self.user)

        response = self.client.post(
            "/api/v1/auth/logout/",
            {"refresh": refresh, "device_token": "mine"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        row = DeviceToken.objects.get(token="mine")
        self.assertFalse(row.is_active)
        self.assertIn("Signed out", row.last_error)

    def test_logout_without_a_device_token_still_works(self):
        refresh = self.login(self.user)
        self.client.force_authenticate(user=self.user)

        response = self.client.post(
            "/api/v1/auth/logout/", {"refresh": refresh}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(DeviceToken.objects.get(token="mine").is_active)

    def test_you_cannot_deactivate_someone_elses_device(self):
        refresh = self.login(self.other)
        self.client.force_authenticate(user=self.other)

        self.client.post(
            "/api/v1/auth/logout/",
            {"refresh": refresh, "device_token": "mine"},
            format="json",
        )
        self.assertTrue(DeviceToken.objects.get(token="mine").is_active)

    def test_a_device_write_failure_does_not_break_the_logout(self):
        """Push bookkeeping must never cost the user their sign-out.

        Deactivation is best-effort by design: a logout that 500s because a device
        row could not be updated is a worse bug than the one it guards against.
        """
        refresh = self.login(self.user)
        self.client.force_authenticate(user=self.user)

        with patch(
            "core.models.device_token.DeviceToken.objects",
            side_effect=RuntimeError("db down"),
        ):
            response = self.client.post(
                "/api/v1/auth/logout/",
                {"refresh": refresh, "device_token": "mine"},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


def at_rate(rate):
    """Override the registration rate.

    ``override_settings(REST_FRAMEWORK=...)`` cannot do this: DRF binds
    ``SimpleRateThrottle.THROTTLE_RATES`` as a class attribute at import time, so
    the value is already frozen before any test runs. Patching the attribute is what
    actually exercises the code path.
    """
    return patch.object(
        ScopedRateThrottle, "THROTTLE_RATES", {"device_token_register": rate}
    )


class SessionLinkTests(DeviceTokenFixtureMixin, APITestCase):
    """A device stops receiving pushes when the login behind it dies.

    This closes the one path no client can close for itself: when a refresh token
    expires, the app has no credentials left to call *any* endpoint with, so it
    cannot deactivate its own device even though it knows it has been signed out.
    Before the link, that device kept receiving alerts for up to the full staleness
    window with nobody signed in on it.
    """

    def setUp(self):
        self.tenant, self.user = self.make_tenant_user("sesslink")

    def sign_in(self, user=None):
        """A real JWT login, not force_authenticate — the link needs a token jti."""
        from rest_framework_simplejwt.tokens import RefreshToken

        from user.services import sessions as session_service

        user = user or self.user
        refresh = RefreshToken.for_user(user)
        access = refresh.access_token
        request = type("R", (), {"headers": {}, "META": {}})()
        session = session_service.create_for_tokens(user, request, refresh, access)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return client, session, str(refresh)

    def register(self, client, token="dev-1"):
        return client.post(
            "/api/v1/core/device-tokens/", {"token": token}, format="json"
        )

    def test_registration_links_the_device_to_the_current_session(self):
        client, session, _ = self.sign_in()
        response = self.register(client)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        self.assertEqual(DeviceToken.objects.get(token="dev-1").session_id, session.pk)

    def test_revoking_the_session_silences_the_device_immediately(self):
        client, session, _ = self.sign_in()
        self.register(client)

        session.revoke()

        row = DeviceToken.objects.get(token="dev-1")
        self.assertFalse(row.is_active)
        self.assertEqual(row.last_error, "Session ended")

    def test_logout_silences_the_device_even_without_naming_it(self):
        """The `device_token` body field is now a fast path, not the only mechanism.

        A client that forgets it — or a tab that closes before the request lands —
        no longer leaves the device subscribed.
        """
        client, _, refresh = self.sign_in()
        self.register(client)

        response = client.post(
            "/api/v1/auth/logout/", {"refresh": refresh}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(DeviceToken.objects.get(token="dev-1").is_active)

    def test_an_expired_session_silences_the_device_on_the_next_sweep(self):
        from user.services import sessions as session_service

        client, session, _ = self.sign_in()
        self.register(client)

        from user.models.user_session import UserSession

        UserSession.objects.filter(pk=session.pk).update(
            expires_at=timezone.now() - timedelta(days=1)
        )

        session_service.sweep()

        self.assertFalse(DeviceToken.objects.get(token="dev-1").is_active)

    def test_the_sweep_retires_devices_before_purging_the_sessions(self):
        """Order is load-bearing.

        `purge_expired_sessions` hard-deletes the session row, and the FK is
        SET_NULL — so purging first would blank the link and lose the only evidence
        that the device was orphaned.
        """
        from user.models.user_session import UserSession
        from user.services import sessions as session_service

        client, session, _ = self.sign_in()
        self.register(client)
        UserSession.objects.filter(pk=session.pk).update(
            expires_at=timezone.now() - timedelta(days=400)
        )

        result = session_service.sweep()

        self.assertEqual(result["devices_deactivated"], 1)
        self.assertEqual(UserSession.all_objects.filter(pk=session.pk).count(), 0)
        self.assertFalse(DeviceToken.objects.get(token="dev-1").is_active)

    def test_re_registering_moves_the_link_to_the_newer_login(self):
        """Otherwise revoking a stale session would silence a device in active use."""
        first_client, first_session, _ = self.sign_in()
        self.register(first_client)

        second_client, second_session, _ = self.sign_in()
        self.register(second_client)

        self.assertEqual(
            DeviceToken.objects.get(token="dev-1").session_id, second_session.pk
        )

        first_session.revoke()
        self.assertTrue(DeviceToken.objects.get(token="dev-1").is_active)

    def test_a_token_with_no_session_is_left_alone(self):
        """Legacy and purged-session tokens must not be swept up as orphans.

        Guards the behaviour, not the implementation: the query happens to exclude
        them via an INNER JOIN today, so removing the explicit `session__isnull`
        clause does not change the outcome. What must never change is that a token
        with no link survives — every device registered before the link existed is
        in that state.
        """
        self.make_token(self.tenant, self.user, "legacy")
        self.assertEqual(token_service.deactivate_for_dead_sessions(), 0)
        self.assertTrue(DeviceToken.objects.get(token="legacy").is_active)

    def test_a_live_session_does_not_retire_its_device(self):
        client, _, _ = self.sign_in()
        self.register(client)
        self.assertEqual(token_service.deactivate_for_dead_sessions(), 0)
        self.assertTrue(DeviceToken.objects.get(token="dev-1").is_active)

    def test_dry_run_previews_orphans_without_retiring_them(self):
        """`--dry-run` has to preview every action, not most of them.

        Reporting orphans as 0 while the real run retires them makes the preview a
        lie about the one class of change an operator is least expecting.
        """
        client, session, _ = self.sign_in()
        self.register(client)
        session.is_active = False
        session.save(update_fields=["is_active"])

        result = token_service.sweep(dry_run=True)

        self.assertEqual(result["orphaned"], 1)
        self.assertTrue(DeviceToken.objects.get(token="dev-1").is_active)

    def test_force_authenticate_resolves_no_session_and_still_registers(self):
        """Registration must never depend on the link resolving.

        Agent tokens are not JWTs, tests authenticate without one, and a session can
        be revoked between authentication and this line. The device is still stored;
        it simply falls back to the staleness rule.
        """
        client = APIClient()
        client.force_authenticate(user=self.user)
        response = self.register(client, token="no-session")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(DeviceToken.objects.get(token="no-session").session_id)

    def test_a_re_registration_without_a_session_keeps_the_existing_link(self):
        client, session, _ = self.sign_in()
        self.register(client)

        blind = APIClient()
        blind.force_authenticate(user=self.user)
        self.register(blind)

        self.assertEqual(DeviceToken.objects.get(token="dev-1").session_id, session.pk)


class RegistrationThrottleTests(DeviceTokenFixtureMixin, APITestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.tenant, self.user = self.make_tenant_user("throttle")
        self.client.force_authenticate(user=self.user)

    def test_registration_is_rate_limited(self):
        with at_rate("2/hour"):
            for index in range(2):
                response = self.client.post(
                    "/api/v1/core/device-tokens/", {"token": f"t-{index}"}, format="json"
                )
                self.assertEqual(response.status_code, status.HTTP_201_CREATED)

            response = self.client.post(
                "/api/v1/core/device-tokens/", {"token": "t-over"}, format="json"
            )
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_reading_the_device_list_is_not_throttled(self):
        """The scope is applied per-action, not viewset-wide.

        Sharing one scope across every action would cap a device list at the
        registration rate, which is the kind of limit that only shows up under real
        use.
        """
        self.client.post("/api/v1/core/device-tokens/", {"token": "t"}, format="json")

        for _ in range(5):
            response = self.client.get("/api/v1/core/device-tokens/")
            self.assertEqual(response.status_code, status.HTTP_200_OK)
