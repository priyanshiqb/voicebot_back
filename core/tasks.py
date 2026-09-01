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


def _mark_notification(notification_log_id, status, error="", configuration=None):
    """Record the real delivery outcome on the NotificationLog row.

    Written from the task, not from the enqueue site: the enqueue only knows the
    message was handed to the broker. A targeted UPDATE (rather than load-modify-
    save) keeps this safe to call from a retrying task.

    ``configuration`` records which SMTP connection carried the attempt. Without
    it, a tenant holding several configurations has no way to tell which one a
    given message went through — which is exactly the question worth asking when
    deliverability changes after a provider switch.
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
    if configuration is not None:
        fields["email_configuration_id"] = configuration.pk
    NotificationLog.objects.filter(pk=notification_log_id).update(**fields)


# Recorded verbatim on the NotificationLog when a tenant has no usable booking
# sender, so "why did no confirmation go out?" is answerable from the row rather
# than from a worker log.
NO_BOOKING_SENDER = (
    "No verified, connected booking email configuration for this tenant; "
    "message not sent. Booking email does not fall back to the platform sender."
)


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
    tenant_id=None,
):
    """
    Send a booking-related email, optionally with an .ics calendar invite.
    Retries with backoff on transient failures.

    **Sends through the tenant's own SMTP configuration, or not at all.** There is
    no platform fallback for booking mail: a confirmation arriving from an address
    the customer has never heard of damages the tenant's brand more than a missing
    one, and quietly lending the platform's sending reputation to a tenant's
    customer mail is the thing tenant SMTP exists to stop.

    The sender is resolved **here**, not at enqueue time. A message can sit in the
    queue through a retry backoff while the configuration behind it is disconnected
    or edited, and the state that matters is the one at the moment of sending.

    Marks its NotificationLog row SENT on success, or FAILED with the error once
    retries are exhausted. Until this existed, the row was marked SENT at enqueue
    time, so a message that failed every retry still reported success and could
    never be re-sent.
    """
    if not recipient_list:
        return

    from core.models.notification_log import NotificationLog
    from tenant.models.TenantSettings import TenantSettings
    from utils.mail import connection as mail_connection

    config = TenantSettings.booking_sender(tenant_id) if tenant_id else None
    if config is None:
        # Not an exception: nothing is broken, the tenant simply has not set up a
        # sender. Retrying would burn the budget and end in the same place, so this
        # is terminal and visible instead.
        logger.warning(
            "booking email skipped for tenant %s: no usable sender", tenant_id
        )
        _mark_notification(
            notification_log_id, NotificationLog.Status.FAILED, error=NO_BOOKING_SENDER
        )
        return

    try:
        message = EmailMultiAlternatives(
            subject=subject,
            body=body,
            from_email=from_email or mail_connection.format_from(config),
            to=list(recipient_list),
            reply_to=[config.reply_to] if config.reply_to else None,
            connection=mail_connection.build_connection(config),
        )
        if html_body:
            message.attach_alternative(html_body, "text/html")
        if ics_content:
            message.attach(ics_filename, ics_content, "text/calendar; method=REQUEST")
        message.send()
        _mark_notification(
            notification_log_id, NotificationLog.Status.SENT, configuration=config
        )
        _touch_configuration_use(config)
        logger.info("Scheduling email sent to %s", recipient_list)
    except Exception as exc:  # noqa: BLE001 — retry on any send failure
        logger.exception("Scheduling email failed for %s", recipient_list)
        permanent = mail_connection.is_permanent(exc)
        if permanent:
            _mark_configuration_failed(config, exc)
        if permanent or self.request.retries >= self.max_retries:
            # Out of retries, or retrying cannot help: record the failure so it is
            # visible and re-sendable rather than sitting as a permanently PENDING
            # row.
            _mark_notification(
                notification_log_id,
                NotificationLog.Status.FAILED,
                error=mail_connection.describe_failure(config, exc),
                configuration=config,
            )
            if permanent:
                return
            raise
        raise self.retry(exc=exc)


def _touch_configuration_use(config):
    """Stamp last_used_at, at most once a minute.

    A targeted UPDATE and a time guard, because a tenant sending 500 reminders
    would otherwise issue 500 writes against one row and serialise the whole batch
    on a single lock.
    """
    from datetime import timedelta

    from django.utils import timezone

    from tenant.models.TenantEmailConfiguration import TenantEmailConfiguration

    now = timezone.now()
    if config.last_used_at and config.last_used_at > now - timedelta(minutes=1):
        return
    TenantEmailConfiguration.objects.filter(pk=config.pk).update(last_used_at=now)


def _mark_configuration_failed(config, exc):
    """Flag a configuration whose credentials or sender are permanently rejected.

    Without this the tenant sees messages disappear with nothing on the
    configuration to explain it — the status would still read VERIFIED from a test
    send that passed weeks ago.
    """
    from tenant.models.TenantEmailConfiguration import TenantEmailConfiguration
    from utils.mail import connection as mail_connection

    TenantEmailConfiguration.objects.filter(pk=config.pk).update(
        status=TenantEmailConfiguration.Status.FAILED,
        last_error=mail_connection.describe_failure(config, exc)[:2000],
    )


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
def purge_notification_logs():
    """Beat task: delete notification rows past their retention window.

    Nothing else prunes this table. Every booking produces a confirmation row plus
    one per reminder offset per participant, none of them are ever removed, and
    each holds customer email addresses — so this is a privacy obligation before it
    is a disk one.

    **Hard delete, via ``all_objects``.** A soft delete would leave the addresses in
    place, which defeats the point, and ``objects`` returns a queryset whose
    ``delete()`` deliberately raises.

    Two rules make it safe:

    * **A row is the idempotency mechanism, not a record of it.** Deleting one frees
      its ``dedup_key`` and re-arms that notification. Rows attached to a booking
      that has not happened yet are therefore never touched, however old they are —
      a confirmation written today for a booking eleven months out must outlive the
      retention window.
    * **Failures are kept longer.** A ``FAILED`` row is the only trace of a message
      that never arrived, and is the thing someone goes looking for when a customer
      complains weeks later.
    """
    from datetime import timedelta

    from django.db.models import Q
    from django.utils import timezone

    from core.models.notification_log import NotificationLog

    now = timezone.now()
    sent_days = int(getattr(settings, "NOTIFICATION_LOG_RETENTION_DAYS", 90))
    failed_days = int(
        getattr(settings, "NOTIFICATION_LOG_FAILED_RETENTION_DAYS", 180)
    )
    batch = int(getattr(settings, "NOTIFICATION_LOG_PURGE_BATCH", 5000))

    deleted = 0
    for status_value, days in (
        (NotificationLog.Status.FAILED, failed_days),
        (None, sent_days),
    ):
        queryset = NotificationLog.all_objects.filter(
            created_at__lt=now - timedelta(days=days)
        )
        if status_value is None:
            queryset = queryset.exclude(status=NotificationLog.Status.FAILED)
        else:
            queryset = queryset.filter(status=status_value)

        # The safety rule above, spelled out as a positive condition rather than
        # an exclude(): what may be deleted is "no booking, or a booking that has
        # already happened". Written this way because the reader has to be able to
        # see the rule without first working out how a nullable join behaves under
        # negation.
        queryset = queryset.filter(
            Q(booking__isnull=True) | Q(booking__start_time__lt=now)
        )

        # Chunked so one run cannot take a table-wide lock or build a delete
        # statement with a million ids in it.
        while True:
            ids = list(queryset.values_list("pk", flat=True)[:batch])
            if not ids:
                break
            removed, _ = NotificationLog.all_objects.filter(pk__in=ids).delete()
            deleted += removed
            if len(ids) < batch:
                break

    logger.info("purged %s notification log rows", deleted)
    return deleted


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


def _run_push(self, notification_log_id, describe, send):
    """Shared body for the push tasks: send, record the outcome, retry on failure.

    Push used to be fire-and-forget — no retries and no record — so a transient FCM
    failure dropped an operational alert with nothing but a warning line to show for
    it. Email has had both since it was written; there was never a reason for push
    to differ.

    "Sent" here means **FCM accepted the message**. There is no per-device delivery
    receipt in this API, so a SENT row is a record of acceptance, not of arrival.
    """
    from core.models.notification_log import NotificationLog

    try:
        sent = send()
    except Exception as exc:  # noqa: BLE001
        if self.request.retries >= self.max_retries:
            _mark_notification(
                notification_log_id, NotificationLog.Status.FAILED, error=str(exc)
            )
            logger.exception("push %s failed permanently: %s", describe, exc)
            return 0
        raise self.retry(exc=exc)

    if sent:
        _mark_notification(notification_log_id, NotificationLog.Status.SENT)
    else:
        # Zero accepted is not an exception: push may be unconfigured, or every
        # device may have been retired. Either way there is nothing to retry, and
        # a FAILED row with the reason beats a SENT row that delivered nothing.
        _mark_notification(
            notification_log_id,
            NotificationLog.Status.FAILED,
            error="No push accepted by FCM (push disabled, or no live device tokens).",
        )
    return sent


@shared_task(base=ReportedTask, bind=True, max_retries=3, default_retry_delay=30)
def send_push_to_user_task(
    self, tenant_id, user_id, title, body, data=None, notification_log_id=None, **envelope
):
    from tenant.models.Tenant import Tenant
    from user.models import CustomUser
    from core.services import push

    tenant = Tenant.objects.filter(pk=tenant_id).first()
    user = CustomUser.objects.filter(pk=user_id).first()
    if tenant is None or user is None:
        return 0
    return _run_push(
        self,
        notification_log_id,
        f"to user {user_id}",
        lambda: push.send_to_user(tenant, user, title, body, data, **envelope),
    )


@shared_task(base=ReportedTask, bind=True, max_retries=3, default_retry_delay=30)
def send_push_to_admins_task(
    self, tenant_id, title, body, data=None, notification_log_id=None, **envelope
):
    from tenant.models.Tenant import Tenant
    from core.services import push

    tenant = Tenant.objects.filter(pk=tenant_id).first()
    if tenant is None:
        return 0
    return _run_push(
        self,
        notification_log_id,
        f"to admins of tenant {tenant_id}",
        lambda: push.notify_tenant_staff(tenant, title, body, data, **envelope),
    )


@shared_task(base=ReportedTask)
def sweep_device_tokens():
    """Beat task: retire device tokens that stopped checking in, then purge dead rows.

    Without this ``core_device_tokens`` grows forever and ``is_active`` decays into
    "was reachable once" — the same pair of problems ``user_sessions`` had. Probing
    is not part of the nightly run: it costs one FCM call per token and the age rule
    already covers the same ground. Run ``purge_device_tokens --verify`` by hand when
    a confirmed answer is worth the calls.
    """
    from core.services import device_tokens as token_service

    result = token_service.sweep()
    return result["deactivated"] + result["purged"]


@shared_task(base=ReportedTask)
def parse_contact_import(session_id):
    """
    Stage a contact import CSV off the request thread.

    A 50k-row file means a blob read, a full parse, and 50k inserts — well past a
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
def cleanup_pending_uploads():
    """Beat task: delete abandoned presigned uploads and the objects they left behind.

    Without this the bucket grows forever from uploads that succeeded but were never
    confirmed — a closed tab is enough. Nothing in the product surfaces those bytes,
    which is exactly why it needs to be scheduled rather than remembered.
    """
    from core.services import uploads as upload_service

    results = upload_service.sweep_pending_uploads()
    return sum(result["deleted"] for result in results)


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


@shared_task(base=ReportedTask, bind=True, max_retries=5, default_retry_delay=30)
def notify_runner_campaign_start(self, campaign_id):
    """Tell the voice runner a campaign is ready to dial.

    Retries only what is worth retrying. A rejected token or a malformed body fails
    identically on every attempt, so those are recorded and dropped immediately rather
    than spending fifteen minutes proving it.

    Backoff is 30s / 60s / 120s / 240s / 480s — roughly fifteen minutes of patience,
    enough to ride out a runner deploy. Anything longer than that is the sweeper's job.
    """
    from core.services import campaign_handoff as handoff_service

    try:
        return handoff_service.notify_campaign_start(campaign_id)
    except handoff_service.PermanentHandoffError as exc:
        handoff_service.mark_handoff_failed(campaign_id, str(exc))
        return None
    except handoff_service.TransientHandoffError as exc:
        if self.request.retries >= self.max_retries:
            # Last attempt: record it before Celery raises MaxRetriesExceeded, or the
            # campaign is left reading PENDING with nothing to say why.
            handoff_service.mark_handoff_failed(
                campaign_id, f"Gave up after {self.max_retries} attempts: {exc}"
            )
            return None
        countdown = exc.retry_after
        if countdown is None:
            countdown = min(30 * 2 ** self.request.retries, 600)
        raise self.retry(exc=exc, countdown=countdown)


@shared_task(base=ReportedTask)
def retry_stale_handoffs():
    """Beat task: re-notify campaigns stuck mid-handoff.

    The safety net under `notify_runner_campaign_start`, for the failures a task retry
    cannot cover — a worker killed mid-task, an `on_commit` hook lost to a restart, or
    a runner outage that outlasted the retry budget. Since the push is the runner's
    only way to discover a campaign, without this a lost notification is a campaign
    that never runs and nobody is told.
    """
    from core.services import campaign_handoff as handoff_service

    campaign_ids = handoff_service.stale_handoffs()
    for campaign_id in campaign_ids:
        notify_runner_campaign_start.delay(str(campaign_id))

    if campaign_ids:
        # Named line, not a generic exception: campaigns sitting here across several
        # sweeps is the condition worth alerting on.
        failure_logger.warning(
            "retry_stale_handoffs: re-notifying %s stalled campaign(s)",
            len(campaign_ids),
            extra={"campaign_ids": [str(cid) for cid in campaign_ids]},
        )
    return len(campaign_ids)
