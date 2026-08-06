import logging

from celery import Task, shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives

logger = logging.getLogger(__name__)

# Dedicated logger so terminal task failures can be alerted on without matching
# every other line this module emits.
failure_logger = logging.getLogger("core.tasks.failures")


class ReportedTask(Task):
    """Base task that makes a permanently-failed task visible.

    Every task here retries a few times and then gives up. Without this hook that
    is silent: a confirmation email, a calendar mirror, or a leave-driven
    reschedule can fail forever with nothing but a stack trace buried in worker
    logs. `on_failure` fires once, after retries are exhausted, and emits one
    structured line to alert on.
    """

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        failure_logger.error(
            "task %s permanently failed after %s retries: %s",
            self.name,
            self.request.retries,
            exc,
            extra={
                "task_name": self.name,
                "task_id": task_id,
                "retries": self.request.retries,
            },
            exc_info=einfo.exc_info if einfo else None,
        )
        super().on_failure(exc, task_id, args, kwargs, einfo)


def _default_from_email():
    sender_name = getattr(settings, "EMAIL_SENDER_NAME", "VoiceBot")
    address = getattr(settings, "EMAIL_HOST_USER", "") or "no-reply@localhost"
    return f"{sender_name} <{address}>"


def _mark_notification(notification_log_id, status, error=""):
    """Record the real delivery outcome on the NotificationLog row.

    Written from the task, not from the enqueue site: the enqueue only knows the
    message was handed to the broker. A targeted UPDATE (rather than load-modify-
    save) keeps this safe to call from a retrying task.
    """
    if not notification_log_id:
        return
    from django.utils import timezone

    from core.models.notification_log import NotificationLog

    fields = {"status": status}
    if status == NotificationLog.Status.SENT:
        fields["sent_at"] = timezone.now()
    if error:
        fields["error"] = error[:2000]
    NotificationLog.objects.filter(pk=notification_log_id).update(**fields)


@shared_task(base=ReportedTask, bind=True, max_retries=3, default_retry_delay=30)
def send_scheduling_email_task(
    self,
    subject,
    body,
    recipient_list,
    html_body=None,
    ics_content=None,
    ics_filename="invite.ics",
    from_email=None,
    notification_log_id=None,
):
    """
    Send a booking-related email, optionally with an .ics calendar invite.
    Retries with backoff on transient failures.

    Marks its NotificationLog row SENT on success, or FAILED with the error once
    retries are exhausted. Until this existed, the row was marked SENT at enqueue
    time, so a message that failed every retry still reported success and could
    never be re-sent.
    """
    if not recipient_list:
        return

    from core.models.notification_log import NotificationLog

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
        _mark_notification(notification_log_id, NotificationLog.Status.SENT)
        logger.info("Scheduling email sent to %s", recipient_list)
    except Exception as exc:  # noqa: BLE001 — retry on any send failure
        logger.exception("Scheduling email failed for %s", recipient_list)
        if self.request.retries >= self.max_retries:
            # Out of retries: record the failure so it is visible and re-sendable
            # rather than sitting as a permanently PENDING row.
            _mark_notification(
                notification_log_id, NotificationLog.Status.FAILED, error=str(exc)
            )
            raise
        raise self.retry(exc=exc)


@shared_task(base=ReportedTask, bind=True, max_retries=3, default_retry_delay=30)
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


@shared_task(base=ReportedTask)
def send_due_reminders():
    """Beat task: send upcoming-booking reminders (idempotent via NotificationLog)."""
    from core.services import reminders

    return reminders.send_due_reminders()


@shared_task(base=ReportedTask)
def cleanup_expired_holds():
    """Beat task: release HELD bookings whose hold has lapsed, freeing their slot."""
    from core.services import bookings as booking_service

    return booking_service.release_expired_holds()


@shared_task(base=ReportedTask, bind=True, max_retries=3, default_retry_delay=30)
def materialize_profile_task(self, profile_id):
    """Rebuild one profile's materialized UTC availability windows.

    Queued from the profile write path. Retried, because a failure here silently
    narrows the host's availability to whatever the last successful run produced.
    """
    from core.models.availability_profile import AvailabilityProfile
    from core.services import availability_materializer as materializer

    profile = (
        AvailabilityProfile.all_objects.filter(pk=profile_id)
        .select_related("timezone", "tenant")
        .prefetch_related("rules")
        .first()
    )
    if profile is None:
        logger.warning("materialize_profile_task: profile %s not found", profile_id)
        return 0
    try:
        return materializer.materialize_profile_from_today(profile)
    except Exception as exc:  # noqa: BLE001
        raise self.retry(exc=exc)


@shared_task(base=ReportedTask)
def materialize_availability_horizon():
    """Beat task: roll every active profile's window horizon forward, purge the past.

    Idempotent. Purge first: windows that ended over a day ago can never be read
    (every engine query is bounded below by ``now``) and are what makes the table
    grow without bound.
    """
    from tenant.models.Tenant import Tenant
    from core.services import availability_materializer as materializer

    purged = materializer.purge_past_windows()
    profiles = windows = 0
    for tenant in Tenant.objects.filter(is_deleted=False).iterator(chunk_size=100):
        try:
            seen, written = materializer.materialize_tenant(tenant)
        except Exception:  # noqa: BLE001
            # Per tenant, so one broken tenant does not stop the horizon rolling
            # forward for every other one.
            logger.exception("availability materialization failed for tenant %s", tenant.id)
            continue
        profiles += seen
        windows += written
    logger.info(
        "availability horizon: purged=%s profiles=%s windows=%s", purged, profiles, windows
    )
    return windows


def _connected_integration(booking):
    from core.models.calendar_integration import CalendarIntegration

    return CalendarIntegration.objects.filter(
        tenant=booking.tenant,
        user=booking.primary_host,
        is_deleted=False,
        sync_status=CalendarIntegration.SyncStatus.CONNECTED,
    ).first()


def _handle_permanent_calendar_error(integration, exc, booking=None):
    """Record a non-retryable provider error and return True if it was handled.

    Retrying a permanent error is how a task burns its budget and then abandons
    the work with no signal: nothing marked the integration broken, nothing
    alerted, and `external_event_id` kept pointing at an event that no longer
    exists — so every subsequent push took the `update_event` branch and failed
    forever.
    """
    from core.models.calendar_integration import CalendarIntegration
    from core.services import calendars

    if not calendars.is_permanent(exc):
        return False

    status = calendars.http_status_of(exc)

    if status in calendars.AUTH_FAILURE_STATUSES:
        # The user must re-consent; this also removes the integration from
        # availability lookups, which filter on CONNECTED.
        CalendarIntegration.objects.filter(pk=integration.pk).update(
            sync_status=CalendarIntegration.SyncStatus.REAUTH_REQUIRED,
            last_error=f"{status}: {exc}"[:500],
        )
        logger.error(
            "calendar integration %s needs re-auth (HTTP %s)", integration.id, status
        )
        return True

    if status in calendars.GONE_STATUSES:
        # The remote event is gone. Clearing the id lets the next push INSERT a
        # fresh event instead of UPDATEing one that no longer exists.
        if booking is not None and booking.external_event_id:
            from core.models.booking import Booking

            Booking.objects.filter(pk=booking.pk).update(external_event_id=None)
            logger.warning(
                "external event for booking %s is gone (HTTP %s); cleared for re-push",
                booking.pk,
                status,
            )
        return True

    CalendarIntegration.objects.filter(pk=integration.pk).update(
        sync_status=CalendarIntegration.SyncStatus.ERROR,
        last_error=f"{status}: {exc}"[:500],
    )
    logger.error(
        "calendar integration %s failed permanently (HTTP %s): %s",
        integration.id,
        status,
        exc,
    )
    return True


@shared_task(base=ReportedTask, bind=True, max_retries=3, default_retry_delay=30)
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
            meeting_url = provider.update_event(integration, booking)
            if meeting_url:
                Booking.objects.filter(pk=booking.pk).update(meeting_url=meeting_url)
        else:
            external_id, meeting_url = provider.push_event(integration, booking)
            Booking.objects.filter(pk=booking.pk).update(
                external_event_id=external_id, 
                external_provider=integration.provider,
                meeting_url=meeting_url
            )
        event_log.log_event(booking, BookingEvent.EventType.EXTERNAL_SYNCED)
        
        logger.info(
            "Successfully synced booking %s to calendar", 
            booking_id, 
            extra={
                "booking_id": str(booking.id), 
                "tenant_id": str(booking.tenant_id), 
                "provider": integration.provider,
                "action": "update" if booking.external_event_id else "insert"
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "push_booking_to_calendar failed for %s", 
            booking_id, 
            extra={
                "booking_id": str(booking.id), 
                "tenant_id": str(booking.tenant_id),
                "provider": integration.provider
            }
        )
        if _handle_permanent_calendar_error(integration, exc, booking=booking):
            return
        raise self.retry(exc=exc)


@shared_task(base=ReportedTask, bind=True, max_retries=3, default_retry_delay=30)
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
        
        logger.info(
            "Successfully deleted external calendar event for booking %s", 
            booking_id,
            extra={
                "booking_id": str(booking.id), 
                "tenant_id": str(booking.tenant_id), 
                "external_event_id": booking.external_event_id,
                "provider": integration.provider
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "delete_booking_calendar_event failed for %s", 
            booking_id,
            extra={
                "booking_id": str(booking.id), 
                "tenant_id": str(booking.tenant_id),
                "external_event_id": booking.external_event_id,
                "provider": integration.provider
            }
        )
        if _handle_permanent_calendar_error(integration, exc, booking=booking):
            # Already-gone counts as done: clear the id so we stop chasing it.
            Booking.objects.filter(pk=booking.pk).update(external_event_id=None)
            return
        raise self.retry(exc=exc)


@shared_task(base=ReportedTask, bind=True, max_retries=3, default_retry_delay=30)
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
        if _handle_permanent_calendar_error(integration, exc):
            return
        raise self.retry(exc=exc)


@shared_task(base=ReportedTask)
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
    for integration in integrations.iterator(chunk_size=200):
        try:
            calendars.get_provider_for(integration).refresh_access_token(integration)
            integration.save(
                update_fields=["access_token", "token_expires_at", "updated_at"]
            )
            refreshed += 1
        except Exception as exc:  # noqa: BLE001
            # Per-integration isolation: one broken account must not stop every
            # later token from being refreshed, or a single bad row would expire
            # the whole tenant's calendars.
            logger.exception("token refresh failed for integration %s", integration.id)
            try:
                integration.sync_status = CalendarIntegration.SyncStatus.REAUTH_REQUIRED
                integration.last_error = str(exc)[:500]
                integration.save(
                    update_fields=["sync_status", "last_error", "updated_at"]
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "could not record refresh failure for integration %s",
                    integration.id,
                )
    return refreshed


@shared_task(base=ReportedTask, bind=True, max_retries=3, default_retry_delay=30)
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
        if _handle_permanent_calendar_error(integration, exc):
            return
        raise self.retry(exc=exc)


@shared_task(base=ReportedTask)
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


@shared_task(base=ReportedTask)
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
    for integration in integrations.iterator(chunk_size=200):
        try:
            if calendar_sync.setup_watch(integration):
                renewed += 1
        except Exception as exc:  # noqa: BLE001
            # Already isolated per item; upgraded to exception() so the traceback
            # survives — a watch that silently stops renewing degrades availability
            # to on-demand free/busy without anything saying so.
            logger.exception("renew watch failed for %s: %s", integration.id, exc)
    return renewed


@shared_task(base=ReportedTask)
def send_push_to_user_task(tenant_id, user_id, title, body, data=None):
    from tenant.models.Tenant import Tenant
    from user.models import CustomUser
    from core.services import push

    tenant = Tenant.objects.filter(pk=tenant_id).first()
    user = CustomUser.objects.filter(pk=user_id).first()
    if tenant is None or user is None:
        return 0
    return push.send_to_user(tenant, user, title, body, data)


@shared_task(base=ReportedTask)
def send_push_to_admins_task(tenant_id, title, body, data=None):
    from tenant.models.Tenant import Tenant
    from core.services import push

    tenant = Tenant.objects.filter(pk=tenant_id).first()
    if tenant is None:
        return 0
    return push.notify_tenant_admins(tenant, title, body, data)


@shared_task(base=ReportedTask)
def parse_contact_import(session_id):
    """
    Stage a contact import CSV off the request thread.

    A 50k-row file means an S3 read, a full parse, and 50k inserts — well past a
    gunicorn worker timeout if it ran inline.
    """
    from core.services import contact_import

    session = contact_import.parse_session(session_id)
    return str(session.status) if session else None


@shared_task(base=ReportedTask)
def commit_contact_import(session_id):
    """
    Write staged rows into Contact.

    The session was already claimed (READY -> COMMITTING) in the request, so this is
    safe to retry: rows carry terminal statuses and are not reprocessed.
    """
    from core.services import contact_import

    session = contact_import.commit_session(session_id)
    return str(session.status) if session else None


@shared_task(base=ReportedTask)
def cleanup_expired_contact_imports():
    """Beat task: purge staging rows and blobs for sessions past their TTL."""
    from core.services import contact_import

    return contact_import.cleanup_expired_sessions()


@shared_task(base=ReportedTask)
def materialize_campaign(campaign_id):
    """Freeze a campaign's audience into CampaignMember rows and start it running."""
    from core.services import campaigns as campaign_service

    campaign = campaign_service.materialize_campaign(campaign_id)
    return str(campaign.status) if campaign else None


@shared_task(base=ReportedTask)
def start_due_campaigns():
    """Beat task: launch campaigns whose scheduled start time has arrived."""
    from core.services import campaigns as campaign_service

    return campaign_service.start_due_campaigns()
