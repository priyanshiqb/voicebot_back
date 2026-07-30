"""
Webhook-driven incremental sync of external calendars into the ExternalBusyBlock
cache, plus watch-channel lifecycle. Tenant-scoped (runs outside the request).
"""

import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from core.models.booking import Booking
from core.models.external_busy_block import ExternalBusyBlock
from core.services import calendars

logger = logging.getLogger(__name__)

# Per-provider setting holding the public webhook URL for push notifications.
_WEBHOOK_URL_SETTINGS = {
    "GOOGLE": "GOOGLE_CALENDAR_WEBHOOK_URL",
    "OUTLOOK": "MICROSOFT_CALENDAR_WEBHOOK_URL",
}


def webhook_url_for(integration):
    """Public notification URL configured for this integration's provider, or ''."""
    setting_name = _WEBHOOK_URL_SETTINGS.get(integration.provider)
    if not setting_name:
        return ""
    return getattr(settings, setting_name, "")


def _our_event_ids(integration, window_start):
    """External ids of events WE pushed for this host — skipped on sync (echo guard)."""
    return set(
        Booking.objects.filter(
            tenant=integration.tenant, 
            primary_host=integration.user, 
            is_deleted=False,
            start_time__gte=window_start
        )
        .exclude(external_event_id__isnull=True)
        .exclude(external_event_id="")
        .values_list("external_event_id", flat=True)
    )


def run_incremental_sync(integration, now=None, horizon_days=60):
    """Apply provider changes to the ExternalBusyBlock cache. Returns count applied."""
    now = now or timezone.now()
    provider = calendars.get_provider_for(integration)
    result = provider.sync_events(integration, now, now + timedelta(days=horizon_days))

    ours = _our_event_ids(integration, now)
    applied = 0
    for change in result.changes:
        if not change.external_id or change.external_id in ours:
            continue  # echo guard: ignore events we created
        if change.cancelled or change.start is None or change.end is None:
            for block in ExternalBusyBlock.objects.filter(
                integration=integration, external_event_id=change.external_id
            ):
                block.delete()  # hard delete (single instance; pure cache row)
        else:
            ExternalBusyBlock.objects.update_or_create(
                integration=integration,
                external_event_id=change.external_id,
                defaults={
                    "tenant": integration.tenant,
                    "user": integration.user,
                    "start_time": change.start,
                    "end_time": change.end,
                },
            )
        applied += 1

    integration.sync_token = result.sync_token
    integration.last_synced_at = now
    integration.last_error = ""
    integration.save(
        update_fields=["sync_token", "last_synced_at", "last_error", "updated_at"]
    )
    return applied


def setup_watch(integration):
    """(Re)establish a push-notification channel. No-op (returns False) if webhooks
    aren't configured for this integration's provider."""
    webhook_url = webhook_url_for(integration)
    if not webhook_url:
        return False
    provider = calendars.get_provider_for(integration)
    if integration.watch_channel_id:
        try:
            provider.stop_watch(integration)
        except Exception as exc:  # noqa: BLE001
            logger.warning("stop_watch during renew failed: %s", exc)
    token = secrets.token_urlsafe(24)
    channel = provider.setup_watch(integration, webhook_url, token)
    integration.watch_channel_id = channel.channel_id
    integration.watch_resource_id = channel.resource_id
    integration.watch_token = token
    integration.watch_expires_at = channel.expiration
    integration.save(
        update_fields=[
            "watch_channel_id",
            "watch_resource_id",
            "watch_token",
            "watch_expires_at",
            "updated_at",
        ]
    )
    return True


def stop_watch(integration):
    if not integration.watch_channel_id:
        return
    try:
        calendars.get_provider_for(integration).stop_watch(integration)
    except Exception as exc:  # noqa: BLE001
        logger.warning("stop_watch failed: %s", exc)
    integration.watch_channel_id = None
    integration.watch_resource_id = None
    integration.watch_token = ""
    integration.watch_expires_at = None
    integration.save(
        update_fields=[
            "watch_channel_id",
            "watch_resource_id",
            "watch_token",
            "watch_expires_at",
            "updated_at",
        ]
    )
