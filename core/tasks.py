import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives

logger = logging.getLogger(__name__)


def _default_from_email():
    sender_name = getattr(settings, "EMAIL_SENDER_NAME", "VoiceBot")
    address = getattr(settings, "EMAIL_HOST_USER", "") or "no-reply@localhost"
    return f"{sender_name} <{address}>"


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_scheduling_email_task(
    self,
    subject,
    body,
    recipient_list,
    html_body=None,
    ics_content=None,
    ics_filename="invite.ics",
    from_email=None,
):
    """
    Send a booking-related email, optionally with an .ics calendar invite.
    Retries with backoff on transient failures.
    """
    if not recipient_list:
        return

    from_email = from_email or _default_from_email()
    try:
        message = EmailMultiAlternatives(
            subject=subject,
            body=body,
            from_email=from_email,
            to=list(recipient_list),
        )
        if html_body:
            message.attach_alternative(html_body, "text/html")
        if ics_content:
            message.attach(ics_filename, ics_content, "text/calendar; method=REQUEST")
        message.send()
        logger.info("Scheduling email sent to %s", recipient_list)
    except Exception as exc:  # noqa: BLE001 — retry on any send failure
        logger.exception("Scheduling email failed for %s", recipient_list)
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def reschedule_on_leave(self, tenant_id, exception_id):
    """
    Handle conflict detection + resolution when an AvailabilityException is
    created. Enqueued (on_commit) by the post_save signal, never run inline in
    the request. Idempotent, so retries are safe.
    """
    from tenant.models.Tenant import Tenant
    from core.services import reschedule as reschedule_service

    tenant = Tenant.objects.filter(pk=tenant_id).first()
    if tenant is None:
        return None
    try:
        return reschedule_service.handle_leave(tenant, exception_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("reschedule_on_leave failed for exception %s", exception_id)
        raise self.retry(exc=exc)


@shared_task
def send_due_reminders():
    """Beat task: send upcoming-booking reminders (idempotent via NotificationLog)."""
    from core.services import reminders

    return reminders.send_due_reminders()


@shared_task
def cleanup_expired_holds():
    """Beat task: release HELD bookings whose hold has lapsed, freeing their slot."""
    from core.services import bookings as booking_service

    return booking_service.release_expired_holds()


def _connected_integration(booking):
    from core.models.calendar_integration import CalendarIntegration

    return CalendarIntegration.objects.filter(
        tenant=booking.tenant,
        user=booking.primary_host,
        is_deleted=False,
        sync_status=CalendarIntegration.SyncStatus.CONNECTED,
    ).first()


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def push_booking_to_calendar(self, booking_id):
    """Mirror a booking to its host's external calendar. Idempotent: updates when
    external_event_id is already set, inserts otherwise. No-op if the host has no
    connected calendar."""
    from core.models.booking import Booking
    from core.models.booking_event import BookingEvent
    from core.services import calendars, events as event_log

    booking = (
        Booking.objects.select_related("tenant", "primary_host", "event_template")
        .filter(pk=booking_id, is_deleted=False)
        .first()
    )
    if booking is None:
        return
    integration = _connected_integration(booking)
    if integration is None:
        return
    try:
        provider = calendars.get_provider_for(integration)
        if booking.external_event_id:
            provider.update_event(integration, booking)
        else:
            external_id = provider.push_event(integration, booking)
            Booking.objects.filter(pk=booking.pk).update(
                external_event_id=external_id, external_provider=integration.provider
            )
        event_log.log_event(booking, BookingEvent.EventType.EXTERNAL_SYNCED)
    except Exception as exc:  # noqa: BLE001
        logger.exception("push_booking_to_calendar failed for %s", booking_id)
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def delete_booking_calendar_event(self, booking_id):
    """Remove a booking's mirrored external event. No-op if never pushed."""
    from core.models.booking import Booking
    from core.services import calendars

    booking = (
        Booking.objects.select_related("tenant", "primary_host")
        .filter(pk=booking_id)
        .first()
    )
    if booking is None or not booking.external_event_id:
        return
    integration = _connected_integration(booking)
    if integration is None:
        return
    try:
        provider = calendars.get_provider_for(integration)
        provider.delete_event(integration, booking.external_event_id)
        Booking.objects.filter(pk=booking.pk).update(external_event_id=None)
    except Exception as exc:  # noqa: BLE001
        logger.exception("delete_booking_calendar_event failed for %s", booking_id)
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def delete_calendar_event_for_host(self, tenant_id, host_id, external_event_id):
    """Delete a specific external event from a specific host's calendar.

    Legacy: its only caller was leave-time auto-reassignment, which was removed
    with the ROUND_ROBIN strategy. Retained for one release so in-flight broker
    messages still resolve; drop it after that."""
    if not external_event_id:
        return
    from core.models.calendar_integration import CalendarIntegration
    from core.services import calendars

    integration = CalendarIntegration.objects.filter(
        tenant_id=tenant_id,
        user_id=host_id,
        is_deleted=False,
        sync_status=CalendarIntegration.SyncStatus.CONNECTED,
    ).first()
    if integration is None:
        return
    try:
        calendars.get_provider_for(integration).delete_event(
            integration, external_event_id
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "delete_calendar_event_for_host failed for host %s", host_id
        )
        raise self.retry(exc=exc)


@shared_task
def refresh_calendar_tokens():
    """Beat task: refresh access tokens that are close to expiry; flag failures
    as REAUTH_REQUIRED."""
    from datetime import timedelta

    from django.utils import timezone

    from core.models.calendar_integration import CalendarIntegration
    from core.services import calendars

    soon = timezone.now() + timedelta(minutes=10)
    integrations = CalendarIntegration.objects.filter(
        is_deleted=False,
        sync_status=CalendarIntegration.SyncStatus.CONNECTED,
        token_expires_at__lt=soon,
    )
    refreshed = 0
    for integration in integrations:
        try:
            calendars.get_provider_for(integration).refresh_access_token(integration)
            integration.save(
                update_fields=["access_token", "token_expires_at", "updated_at"]
            )
            refreshed += 1
        except Exception as exc:  # noqa: BLE001
            integration.sync_status = CalendarIntegration.SyncStatus.REAUTH_REQUIRED
            integration.last_error = str(exc)[:500]
            integration.save(
                update_fields=["sync_status", "last_error", "updated_at"]
            )
    return refreshed


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def incremental_calendar_sync(self, integration_id):
    """Webhook-triggered: pull changes into the ExternalBusyBlock cache."""
    from core.models.calendar_integration import CalendarIntegration
    from core.services import calendar_sync

    integration = CalendarIntegration.objects.filter(
        pk=integration_id,
        is_deleted=False,
        sync_status=CalendarIntegration.SyncStatus.CONNECTED,
    ).first()
    if integration is None:
        return
    try:
        return calendar_sync.run_incremental_sync(integration)
    except Exception as exc:  # noqa: BLE001
        logger.exception("incremental_calendar_sync failed for %s", integration_id)
        raise self.retry(exc=exc)


@shared_task
def establish_calendar_watch(integration_id):
    """After connect: set up a push channel and seed the cache. No-op if webhooks
    aren't configured for the integration's provider."""
    from core.models.calendar_integration import CalendarIntegration
    from core.services import calendar_sync

    integration = CalendarIntegration.objects.filter(
        pk=integration_id,
        is_deleted=False,
        sync_status=CalendarIntegration.SyncStatus.CONNECTED,
    ).first()
    if integration is None:
        return
    if not calendar_sync.webhook_url_for(integration):
        return
    try:
        calendar_sync.setup_watch(integration)
        calendar_sync.run_incremental_sync(integration)
    except Exception as exc:  # noqa: BLE001
        logger.warning("establish_calendar_watch failed for %s: %s", integration_id, exc)


@shared_task
def renew_calendar_watches():
    """Beat task: renew watch channels that are close to expiry."""
    from datetime import timedelta

    from django.utils import timezone

    from core.models.calendar_integration import CalendarIntegration
    from core.services import calendar_sync

    soon = timezone.now() + timedelta(hours=6)
    integrations = CalendarIntegration.objects.filter(
        is_deleted=False,
        sync_status=CalendarIntegration.SyncStatus.CONNECTED,
        watch_channel_id__isnull=False,
        watch_expires_at__lt=soon,
    )
    renewed = 0
    for integration in integrations:
        try:
            if calendar_sync.setup_watch(integration):
                renewed += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("renew watch failed for %s: %s", integration.id, exc)
    return renewed


@shared_task
def send_push_to_user_task(tenant_id, user_id, title, body, data=None):
    from tenant.models.Tenant import Tenant
    from user.models import CustomUser
    from core.services import push

    tenant = Tenant.objects.filter(pk=tenant_id).first()
    user = CustomUser.objects.filter(pk=user_id).first()
    if tenant is None or user is None:
        return 0
    return push.send_to_user(tenant, user, title, body, data)


@shared_task
def send_push_to_admins_task(tenant_id, title, body, data=None):
    from tenant.models.Tenant import Tenant
    from core.services import push

    tenant = Tenant.objects.filter(pk=tenant_id).first()
    if tenant is None:
        return 0
    return push.notify_tenant_admins(tenant, title, body, data)


@shared_task
def parse_contact_import(session_id):
    """
    Stage a contact import CSV off the request thread.

    A 50k-row file means an S3 read, a full parse, and 50k inserts — well past a
    gunicorn worker timeout if it ran inline.
    """
    from core.services import contact_import

    session = contact_import.parse_session(session_id)
    return str(session.status) if session else None


@shared_task
def commit_contact_import(session_id):
    """
    Write staged rows into Contact.

    The session was already claimed (READY -> COMMITTING) in the request, so this is
    safe to retry: rows carry terminal statuses and are not reprocessed.
    """
    from core.services import contact_import

    session = contact_import.commit_session(session_id)
    return str(session.status) if session else None


@shared_task
def cleanup_expired_contact_imports():
    """Beat task: purge staging rows and blobs for sessions past their TTL."""
    from core.services import contact_import

    return contact_import.cleanup_expired_sessions()
