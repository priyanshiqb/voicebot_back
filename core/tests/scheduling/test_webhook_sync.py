from datetime import date, timedelta
from unittest import skipUnless
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.models.booking import Booking
from core.models.external_busy_block import ExternalBusyBlock
from core.services import availability, bookings as booking_service, calendar_sync
from core.services.calendars.base import ChangedEvent, SyncResult, WatchChannel
from core.tests.scheduling import factories as f
from core.tests.scheduling.factories import ATTENDEES, NOW, at
from core.tests.scheduling.test_calendar import make_integration

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)

WEBHOOK_URL = "/api/v1/core/calendar-webhook/google/"


class FakeSyncProvider:
    def __init__(self, changes=None, sync_token="tok-new"):
        self.changes = changes or []
        self.sync_token = sync_token
        self.watch_setup = 0
        self.stopped = 0

    def get_busy(self, integration, window_start, window_end):
        return []

    def sync_events(self, integration, window_start, window_end):
        return SyncResult(changes=list(self.changes), sync_token=self.sync_token)

    def setup_watch(self, integration, webhook_url, token):
        self.watch_setup += 1
        return WatchChannel(channel_id="chan-1", resource_id="res-1", expiration=None)

    def stop_watch(self, integration):
        self.stopped += 1


@REQUIRES_PG
class IncrementalSyncTests(TestCase):
    def setUp(self):
        cache.clear()
        self.tenant = f.make_tenant()
        self.template = f.make_template(
            self.tenant, duration_minutes=30, slot_granularity_minutes=30
        )
        self.host = f.make_host(self.tenant, self.template)
        self.integration = make_integration(self.tenant, self.host)

    @patch("core.services.calendars.get_provider_for")
    def test_incremental_sync_upserts_busy_blocks(self, mock_provider):
        mock_provider.return_value = FakeSyncProvider(
            changes=[ChangedEvent("ev-1", False, at(10), at(11))], sync_token="tok-2"
        )
        applied = calendar_sync.run_incremental_sync(self.integration, now=NOW)
        self.assertEqual(applied, 1)
        self.assertTrue(
            ExternalBusyBlock.objects.filter(
                integration=self.integration, external_event_id="ev-1"
            ).exists()
        )
        self.integration.refresh_from_db()
        self.assertEqual(self.integration.sync_token, "tok-2")

    @patch("core.services.calendars.get_provider_for")
    def test_echo_guard_skips_our_events(self, mock_provider):
        mock_provider.return_value = FakeSyncProvider()  # get_busy -> [] for create
        booking = booking_service.create_booking(
            tenant=self.tenant,
            event_template=self.template,
            start_time=at(10),
            participants=ATTENDEES,
            target_host_id=self.host.id,
            now=NOW,
        )
        Booking.objects.filter(pk=booking.pk).update(external_event_id="ev-ours")

        mock_provider.return_value = FakeSyncProvider(
            changes=[ChangedEvent("ev-ours", False, at(10), at(11))]
        )
        applied = calendar_sync.run_incremental_sync(self.integration, now=NOW)
        self.assertEqual(applied, 0)
        self.assertFalse(
            ExternalBusyBlock.objects.filter(external_event_id="ev-ours").exists()
        )

    @patch("core.services.calendars.get_provider_for")
    def test_cancelled_event_removes_block(self, mock_provider):
        ExternalBusyBlock.objects.create(
            tenant=self.tenant,
            user=self.host,
            integration=self.integration,
            external_event_id="ev-x",
            start_time=at(10),
            end_time=at(11),
        )
        mock_provider.return_value = FakeSyncProvider(
            changes=[ChangedEvent("ev-x", True, None, None)]
        )
        calendar_sync.run_incremental_sync(self.integration, now=NOW)
        self.assertFalse(
            ExternalBusyBlock.objects.filter(external_event_id="ev-x").exists()
        )

    @patch("core.services.calendars.get_provider_for")
    def test_availability_reads_cache_when_watch_live(self, mock_provider):
        self.integration.watch_channel_id = "chan-1"
        self.integration.watch_expires_at = NOW + timedelta(days=1)  # 2027 > real now
        self.integration.save()
        ExternalBusyBlock.objects.create(
            tenant=self.tenant,
            user=self.host,
            integration=self.integration,
            external_event_id="ev-1",
            start_time=at(10),
            end_time=at(10, 30),
        )
        slots = availability.get_available_slots(
            self.tenant, self.template, *f.utc_day(date(2027, 3, 16)),
            target_host_id=self.host.id, now=NOW,
        )
        starts = {s.start for s in slots}
        self.assertNotIn(at(10), starts)
        mock_provider.assert_not_called()  # served from cache, no live call


@REQUIRES_PG
class WatchLifecycleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.tenant = f.make_tenant()
        self.template = f.make_template(self.tenant)
        self.host = f.make_host(self.tenant, self.template)
        self.integration = make_integration(self.tenant, self.host)

    # Pinned, not ambient: this asserts behaviour when no webhook URL is configured,
    # so it must not depend on whether the developer's .env happens to set one.
    @override_settings(GOOGLE_CALENDAR_WEBHOOK_URL="")
    @patch("core.services.calendars.get_provider_for")
    def test_setup_watch_noop_without_config(self, mock_provider):
        self.assertFalse(calendar_sync.setup_watch(self.integration))
        mock_provider.assert_not_called()

    @override_settings(GOOGLE_CALENDAR_WEBHOOK_URL="https://example.com/webhook")
    @patch("core.services.calendars.get_provider_for")
    def test_setup_watch_when_configured(self, mock_provider):
        fake = FakeSyncProvider()
        mock_provider.return_value = fake
        self.assertTrue(calendar_sync.setup_watch(self.integration))
        self.integration.refresh_from_db()
        self.assertEqual(self.integration.watch_channel_id, "chan-1")
        self.assertTrue(self.integration.watch_token)
        self.assertEqual(fake.watch_setup, 1)


@REQUIRES_PG
class WebhookEndpointTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(self.tenant)
        self.host = f.make_host(self.tenant, self.template)
        self.integration = make_integration(self.tenant, self.host)
        self.integration.watch_channel_id = "chan-abc"
        self.integration.watch_token = "tok-secret"
        self.integration.save()
        self.client = APIClient()

    def _post(self, channel, token, state):
        return self.client.post(
            WEBHOOK_URL,
            **{
                "HTTP_X_GOOG_CHANNEL_ID": channel,
                "HTTP_X_GOOG_CHANNEL_TOKEN": token,
                "HTTP_X_GOOG_RESOURCE_STATE": state,
            },
        )

    def test_valid_notification_triggers_sync(self):
        with patch("core.tasks.incremental_calendar_sync.delay") as mock_delay:
            response = self._post("chan-abc", "tok-secret", "exists")
            self.assertEqual(response.status_code, 200)
            mock_delay.assert_called_once_with(str(self.integration.id))

    def test_invalid_token_ignored(self):
        with patch("core.tasks.incremental_calendar_sync.delay") as mock_delay:
            response = self._post("chan-abc", "wrong-token", "exists")
            self.assertEqual(response.status_code, 200)
            mock_delay.assert_not_called()

    def test_sync_handshake_is_noop(self):
        with patch("core.tasks.incremental_calendar_sync.delay") as mock_delay:
            response = self._post("chan-abc", "tok-secret", "sync")
            self.assertEqual(response.status_code, 200)
            mock_delay.assert_not_called()

    def test_unknown_channel_ignored(self):
        with patch("core.tasks.incremental_calendar_sync.delay") as mock_delay:
            response = self._post("chan-unknown", "tok-secret", "exists")
            self.assertEqual(response.status_code, 200)
            mock_delay.assert_not_called()
