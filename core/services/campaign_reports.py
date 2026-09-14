"""Campaign reports: what goes into one, and the job that builds it.

One entry point per actor:

* ``request_report`` — the API. Writes a PENDING row and schedules the work. Nothing
  expensive happens inside a request.
* ``build_report`` — the worker. Streams the audience into a file, uploads it, and tells
  the person who asked.
* ``expire_reports`` and ``fail_stalled_reports`` — the beat sweeps.

What a report *contains* is decided here, once. ``core.services.report_writers`` only
lays it out, which is what keeps the workbook and the PDF in agreement.
"""

import datetime as dt
import logging
import os
import tempfile
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import transaction
from django.db.models import Avg, Count, OuterRef, Q, Subquery, Sum
from django.utils import timezone
from django.utils.text import slugify

from core.constants.service_tier import ServiceTier
from core.models.agent_call_log import AgentCallLog
from core.models.campaign import Campaign
from core.models.campaign_member import CampaignMember
from core.models.campaign_report import CampaignReport
from core.models.in_app_notification import InAppNotification
from core.models.notification_log import NotificationLog
from core.services.report_writers import DATETIME, NUMBER, Column, ReportContent
from utils.blob_storage.keys import build_tenant_object_key
from utils.blob_storage.service import AzureBlobStorageService

logger = logging.getLogger(__name__)

Status = CampaignReport.Status
Format = CampaignReport.Format

REPORT_FOLDER = "campaign-reports"

CONTENT_TYPES = {
    Format.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    Format.PDF: "application/pdf",
}
EXTENSIONS = {Format.XLSX: "xlsx", Format.PDF: "pdf"}

#: Shown to the user when a report fails for a reason they cannot act on. The detail
#: goes to the logs; a stack trace in a notification helps nobody.
GENERIC_FAILURE_MESSAGE = "The report could not be generated. Please request it again."
STALLED_MESSAGE = "Report generation stopped before it finished. Please request it again."


class ReportError(Exception):
    """Base for report failures."""


class PermanentReportError(ReportError):
    """Retrying cannot help — the campaign is gone, the format is unknown."""


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

def retention_days():
    return int(getattr(settings, "CAMPAIGN_REPORT_RETENTION_DAYS", 7))


def progress_every():
    return max(1, int(getattr(settings, "CAMPAIGN_REPORT_PROGRESS_EVERY", 500)))


def pdf_max_rows():
    return int(getattr(settings, "CAMPAIGN_REPORT_PDF_MAX_ROWS", 2000))


def stall_minutes():
    return int(getattr(settings, "CAMPAIGN_REPORT_STALL_MINUTES", 120))


def download_url_hours():
    return int(getattr(settings, "CAMPAIGN_REPORT_DOWNLOAD_URL_HOURS", 1))


# --------------------------------------------------------------------------- #
# Requesting
# --------------------------------------------------------------------------- #

def request_report(campaign, user, report_format=Format.XLSX):
    """Record that a report was asked for, and schedule the work. Returns the row.

    The row is written here, by the request, rather than by the worker. The id can then
    be returned for polling at once — and a backed-up queue does not look identical to a
    request that silently failed.

    Enqueued on commit: a task fired before its row is committed looks up an id that does
    not exist yet, and on a fast broker the worker wins that race.
    """
    from core.tasks import generate_campaign_report

    if report_format not in Format.values:
        raise ValueError(f"Unknown report format {report_format!r}.")

    with transaction.atomic():
        report = CampaignReport.objects.create(
            tenant_id=campaign.tenant_id,
            campaign=campaign,
            report_format=report_format,
            created_by=user,
            updated_by=user,
        )
        transaction.on_commit(lambda: generate_campaign_report.delay(str(report.pk)))
    return report


def download_payload(report):
    """A fresh, short-lived download link. Minted per request and never stored.

    A stored URL would be a credential with an expiry nobody tracks. The link is named
    after the report so the file lands in a downloads folder as something recognisable,
    not as the blob's tenant-prefixed key.
    """
    hours = download_url_hours()
    url = AzureBlobStorageService().get_presigned_download_url(
        report.file_key, report.file_name, expiry_hours=hours
    )
    return {
        "download_url": url,
        "file_name": report.file_name,
        "file_size_bytes": report.file_size_bytes,
        "expires_in_seconds": hours * 3600,
    }


# --------------------------------------------------------------------------- #
# What a report contains
# --------------------------------------------------------------------------- #

def report_timezone(campaign):
    """The zone a report's times are shown in, as ``(name, ZoneInfo)``.

    The campaign's own schedule zone first, since that is the zone its audience was
    dialled in; then the tenant's; then UTC. Decided once per report because Excel has no
    aware datetimes — every time in the file has to be in one named zone, and the headers
    say which.
    """
    candidates = [campaign.schedule_timezone]
    tenant_zone = getattr(campaign.tenant, "timezone", None)
    if tenant_zone is not None:
        candidates.append(getattr(tenant_zone, "timezone", None))

    for name in candidates:
        if not name:
            continue
        try:
            return name, ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("campaign %s: unknown timezone %r", campaign.pk, name)
    return "UTC", ZoneInfo("UTC")


def _local(value, zone):
    """An aware datetime as a naive one in ``zone``, or None."""
    if value is None:
        return None
    if timezone.is_naive(value):
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(zone).replace(tzinfo=None)


def _person(user):
    if user is None:
        return None
    return f"{user.first_name} {user.last_name}".strip() or user.email


def _duration(seconds):
    if not seconds:
        return None
    seconds = int(round(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s"


def _pct(part, whole):
    if not whole:
        return None
    return f"{part * 100 / whole:.1f}%"


def _tier_label(tier):
    if not tier:
        # Null means the runner did not report a tier and billing used the bot's.
        return "the bot's configured tier"
    try:
        return ServiceTier(tier).label
    except ValueError:
        return tier


def member_columns(zone_name):
    """The member sheet, in order. The time column's header names its zone."""
    return [
        Column("member_id", "Member ID", 38),
        Column("name", "Name", 24),
        Column("company", "Company", 22),
        Column("phone", "Phone", 18),
        Column("status", "Status", 16),
        Column("suppression_reason", "Suppression reason", 28),
        Column("outcome", "Outcome", 28),
        Column("attempts", "Attempts", 10, NUMBER),
        Column("last_call_at", f"Last call at ({zone_name})", 24, DATETIME),
        Column("duration_seconds", "Duration (seconds)", 14, NUMBER),
        Column("credits_charged", "Credits charged", 14, NUMBER),
        Column("error_message", "Error", 32),
        Column("provider_call_id", "Provider call ID", 38),
    ]


def member_queryset(campaign):
    """Every member, with its latest call's figures annotated in the same scan.

    ``AgentCallLog`` points *at* ``CampaignMember``, so there is no ``select_related`` for
    it, and prefetching call logs for 100k members would defeat the streaming. A
    correlated subquery per column runs inside the one query instead, against the
    ``campaign_member`` index.

    The *latest* call when a member was dialled more than once; ``attempts`` says how
    many there were. ``credits_charged`` being a column on the log is what makes credits
    an ordinary annotation here rather than a suffix-aware ledger lookup.

    Same order as the members endpoint's default plus its tiebreaker, so a report and the
    screen it was requested from list people identically.
    """
    latest_call = AgentCallLog.objects.filter(
        campaign_member=OuterRef("pk")
    ).order_by("-created_at")

    return (
        CampaignMember.objects.filter(campaign=campaign)
        .select_related("contact")
        .annotate(
            call_duration=Subquery(latest_call.values("duration_seconds")[:1]),
            call_credits=Subquery(latest_call.values("credits_charged")[:1]),
            call_reference=Subquery(latest_call.values("provider_call_id")[:1]),
        )
        .order_by("created_at", "id")
    )


def _status_label(code):
    """``IN_PROGRESS`` as ``In progress``.

    Not the models' choice labels: those are written for operators ("Runner reported a
    finished call", "Handing off to runner"), and a report is read by the tenant's own
    client, who has never heard of a runner.
    """
    return str(code or "").replace("_", " ").capitalize()


def member_rows(campaign, zone, limit=None):
    """One tuple per member, streamed. Suppressed members included, deliberately.

    Everywhere else in the campaign module suppressed people are hidden — the runner is
    never told they exist. A tenant asking what happened to their campaign is owed "these
    people were not called, and here is why", which is the point of recording a reason.
    """
    queryset = member_queryset(campaign)
    if limit is not None:
        queryset = queryset[:limit]

    for member in queryset.iterator(chunk_size=2000):
        contact = member.contact
        yield (
            str(member.pk),
            contact.full_name if contact else "",
            contact.company if contact else "",
            # The number as frozen at materialisation — what was actually dialled, not
            # whatever the contact's number has been edited to since.
            member.phone_e164,
            _status_label(member.status),
            member.get_suppression_reason_display() if member.suppression_reason else "",
            member.outcome,
            member.attempt_count,
            _local(member.last_call_at, zone),
            member.call_duration,
            member.call_credits,
            member.error_message,
            member.call_reference,
        )


def build_overview(campaign, zone, zone_name, report, generated_at, included):
    """The overview sheet: the campaign as a whole, grouped for reading.

    Member counts come from the member rows rather than the campaign's counters. The
    counters have no cancelled figure, so counts drawn from rows are the only way the
    statuses reconcile exactly to the total on a cancelled campaign.
    """
    members = CampaignMember.objects.filter(campaign=campaign)
    by_status = dict(
        members.values_list("status").annotate(n=Count("id")).order_by()
    )
    by_reason = dict(
        members.exclude(suppression_reason="")
        .values_list("suppression_reason")
        .annotate(n=Count("id"))
        .order_by()
    )
    reason_labels = dict(CampaignMember.SuppressionReason.choices)

    member_status = CampaignMember.Status
    total = sum(by_status.values())
    suppressed = by_status.get(member_status.SUPPRESSED, 0)
    callable_members = total - suppressed
    completed = by_status.get(member_status.COMPLETED, 0)
    failed = by_status.get(member_status.FAILED, 0)

    calls = AgentCallLog.objects.filter(campaign_member__campaign=campaign)
    stats = calls.aggregate(
        total=Count("id"),
        connected=Count("id", filter=Q(duration_seconds__gt=0)),
        talk=Sum("duration_seconds"),
        average=Avg("duration_seconds", filter=Q(duration_seconds__gt=0)),
        credits=Sum("credits_charged"),
        charged=Count("id", filter=Q(credits_charged__isnull=False)),
    )
    by_tier = (
        calls.filter(credits_charged__isnull=False)
        .values_list("service_tier")
        .annotate(credits=Sum("credits_charged"))
        .order_by("service_tier")
    )
    credits_total = stats["credits"] or 0

    return [
        ("Campaign", [
            ("Name", campaign.name),
            ("Description", campaign.description),
            ("Status", _status_label(campaign.status)),
            ("Bot", campaign.agent.bot_name if campaign.agent_id else None),
            ("Audience source", campaign.get_audience_source_display()),
            ("Created at", _local(campaign.created_at, zone)),
            ("Launched by", _person(campaign.launched_by)),
        ]),
        ("Schedule", [
            ("Scheduled start", _local(campaign.scheduled_start_at, zone)),
            ("Schedule timezone", campaign.schedule_timezone or None),
            ("Started at", _local(campaign.started_at, zone)),
            ("Completed at", _local(campaign.completed_at, zone)),
            ("Elapsed", _duration(
                (campaign.completed_at - campaign.started_at).total_seconds()
                if campaign.started_at and campaign.completed_at else None
            )),
        ]),
        ("Audience", [
            ("Total members", total),
            ("Callable", callable_members),
            ("Suppressed", suppressed),
            *[
                (f"Suppressed: {reason_labels.get(reason, reason)}", count)
                for reason, count in sorted(by_reason.items())
            ],
        ]),
        ("Outcomes", [
            ("Completed", completed),
            ("Failed", failed),
            ("Pending", by_status.get(member_status.PENDING, 0)),
            ("In progress", by_status.get(member_status.IN_PROGRESS, 0)),
            ("Cancelled", by_status.get(member_status.CANCELLED, 0)),
            ("Reach rate", _pct(completed, callable_members)),
            ("Failure rate", _pct(failed, callable_members)),
        ]),
        ("Calls", [
            ("Total calls", stats["total"]),
            ("Connected", stats["connected"]),
            ("Connect rate", _pct(stats["connected"], stats["total"])),
            ("Total talk time", _duration(stats["talk"])),
            ("Average duration", _duration(stats["average"])),
        ]),
        ("Credits", [
            ("Credits charged", credits_total),
            ("Calls charged", stats["charged"]),
            (
                "Average per charged call",
                round(credits_total / stats["charged"], 1) if stats["charged"] else None,
            ),
            *[(f"Charged on {_tier_label(tier)}", credits) for tier, credits in by_tier],
            ("Note", "Credits as charged, not net of refunds. The wallet ledger is the record of money."),
        ]),
        ("Report", [
            ("Report ID", str(report.pk)),
            ("Generated at", _local(generated_at, zone)),
            ("Requested by", _person(report.created_by)),
            ("Members in campaign", report.total_rows),
            ("Members included", included),
            ("Times shown in", zone_name),
        ]),
    ]


def report_file_name(campaign, report_format, when):
    """``campaign-<slug>-<YYYYMMDD-HHMMSS>.<ext>``.

    ``when`` is the report's creation time, not the moment of writing, so every retry of
    one report produces the same name and therefore the same blob key.
    """
    slug = slugify(campaign.name)[:60] or "campaign"
    return f"campaign-{slug}-{when:%Y%m%d-%H%M%S}.{EXTENSIONS[report_format]}"


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #

def _claim(report_id):
    """Move a PENDING report to RUNNING and return it, or None if it is not PENDING.

    Anything other than PENDING is skipped, so a task Celery delivers twice cannot
    rebuild a report that is already running, finished or failed.
    """
    with transaction.atomic():
        report = (
            CampaignReport.objects.select_for_update()
            .filter(pk=report_id)
            .first()
        )
        if report is None:
            logger.warning("campaign report %s not found", report_id)
            return None
        if report.status != Status.PENDING:
            logger.info(
                "campaign report %s is %s, not PENDING; skipping", report_id, report.status
            )
            return None

        report.status = Status.RUNNING
        report.started_at = timezone.now()
        report.processed_rows = 0
        report.total_rows = CampaignMember.objects.filter(
            campaign_id=report.campaign_id
        ).count()
        report.save(update_fields=[
            "status", "started_at", "processed_rows", "total_rows", "updated_at",
        ])
    return report


def build_report(report_id):
    """Build one report end to end. Returns the report id, or None if there was nothing to do.

    On any failure the row is handed back to PENDING before the error propagates, so the
    task's retry can claim it again — a retry that found the row still RUNNING would skip
    its own report. Whether there *is* a retry is the task's decision; when there is not,
    the task marks the row FAILED.
    """
    report = _claim(report_id)
    if report is None:
        return None

    try:
        return _generate(report)
    except Exception:
        CampaignReport.objects.filter(pk=report.pk, status=Status.RUNNING).update(
            status=Status.PENDING, processed_rows=0, updated_at=timezone.now()
        )
        raise


def _progress_writer(report_id):
    def on_progress(rows_written):
        CampaignReport.objects.filter(pk=report_id, status=Status.RUNNING).update(
            processed_rows=rows_written, updated_at=timezone.now()
        )

    return on_progress


def _write(report, content, path):
    on_progress = _progress_writer(report.pk)
    if report.report_format == Format.XLSX:
        from core.services.report_writers.xlsx import write_xlsx

        return write_xlsx(
            content, path, on_progress=on_progress, progress_every=progress_every()
        )
    if report.report_format == Format.PDF:
        from core.services.report_writers.pdf import write_pdf

        return write_pdf(
            content, path, on_progress=on_progress, progress_every=progress_every()
        )
    raise PermanentReportError(f"Unknown report format {report.report_format!r}.")


def _generate(report):
    campaign = (
        Campaign.objects.select_related("tenant", "tenant__timezone", "agent", "launched_by")
        .filter(pk=report.campaign_id, is_deleted=False)
        .first()
    )
    if campaign is None:
        raise PermanentReportError(
            "The campaign was deleted before its report could be generated."
        )

    zone_name, zone = report_timezone(campaign)
    limit = pdf_max_rows() if report.report_format == Format.PDF else None
    included = report.total_rows if limit is None else min(report.total_rows, limit)
    generated_at = timezone.now()

    content = ReportContent(
        title=f"Campaign report: {campaign.name}",
        subtitle=(
            f"Generated {_local(generated_at, zone):%d %b %Y %H:%M} · "
            f"times shown in {zone_name}"
        ),
        timezone_name=zone_name,
        overview=build_overview(
            campaign, zone, zone_name, report, generated_at, included
        ),
        columns=member_columns(zone_name),
        rows=member_rows(campaign, zone, limit=limit),
        total_rows=report.total_rows,
    )

    file_name = report_file_name(campaign, report.report_format, report.created_at)
    key = build_tenant_object_key(
        campaign.tenant_id, REPORT_FOLDER, file_name, unique_id=str(report.pk)
    )
    storage = AzureBlobStorageService()

    # A named temp file rather than an in-memory buffer: write-only openpyxl spools rows
    # to disk anyway, and holding the finished file in memory would undo the saving.
    # delete=False and an explicit remove, because Windows will not let a library reopen
    # a NamedTemporaryFile that is still held open.
    handle, path = tempfile.mkstemp(suffix=f".{EXTENSIONS[report.report_format]}")
    os.close(handle)
    try:
        written = _write(report, content, path)
        size = os.path.getsize(path)
        # Upload last. A job that fails before this line leaves nothing downloadable.
        with open(path, "rb") as file_obj:
            storage.upload_file(
                key, file_obj, content_type=CONTENT_TYPES[report.report_format]
            )
    finally:
        try:
            os.remove(path)
        except OSError:
            logger.warning("could not remove report temp file %s", path)

    completed_at = timezone.now()
    expires_at = completed_at + timedelta(days=retention_days())

    updated = CampaignReport.objects.filter(pk=report.pk, status=Status.RUNNING).update(
        status=Status.COMPLETED,
        file_key=key,
        file_name=file_name,
        file_size_bytes=size,
        processed_rows=written,
        rows_included=written,
        completed_at=completed_at,
        expires_at=expires_at,
        updated_at=completed_at,
    )
    if not updated:
        # Something moved the row while this worked — the stall sweep, most likely. No row
        # refers to the file just uploaded, so remove it rather than leak it.
        logger.warning(
            "campaign report %s finished but was no longer RUNNING; discarding its file",
            report.pk,
        )
        storage.delete_blob_direct(key)
        return None

    _notify(
        report,
        title="Your campaign report is ready",
        body=(
            f"The {report.get_report_format_display()} report for “{campaign.name}” "
            f"is ready to download until {_local(expires_at, zone):%d %b %Y}."
        ),
    )
    return str(report.pk)


def mark_failed(report_id, message):
    """Record a report as FAILED and tell the requester. Returns whether it changed.

    Only an open report can fail — a report that already completed stays completed even
    if a late retry fails behind it.
    """
    now = timezone.now()
    updated = CampaignReport.objects.filter(
        pk=report_id, status__in=CampaignReport.OPEN_STATUSES
    ).update(status=Status.FAILED, error_message=message, completed_at=now, updated_at=now)

    if updated:
        report = (
            CampaignReport.objects.select_related("campaign", "created_by")
            .filter(pk=report_id)
            .first()
        )
        if report is not None:
            _notify(
                report,
                title="Your campaign report could not be generated",
                body=(
                    f"The {report.get_report_format_display()} report for "
                    f"“{report.campaign.name}” failed: {message}"
                ),
            )
    return bool(updated)


def _notify(report, *, title, body):
    """An in-app notification for whoever asked. Never allowed to fail the report.

    Only the requester: somebody else's report finishing is not news. A notification
    that could not be written is a missed prompt, not a lost report — the file is already
    stored and the row already says so.
    """
    if report.created_by_id is None:
        return
    try:
        InAppNotification.objects.create(
            tenant_id=report.tenant_id,
            user_id=report.created_by_id,
            title=title,
            body=body,
            notification_type=NotificationLog.NotificationType.REPORTS,
        )
    except Exception:
        logger.exception("could not notify the requester of campaign report %s", report.pk)


# --------------------------------------------------------------------------- #
# Sweeps
# --------------------------------------------------------------------------- #

def expire_reports(now=None):
    """Delete files past their retention window and mark their rows EXPIRED. Returns a count.

    The blob is deleted *before* the row moves. If the delete fails the row stays
    COMPLETED and the next run tries again; the other order would forget the blob exists.
    The row itself survives, so "this report existed" stays answerable without paying to
    store the file.
    """
    now = now or timezone.now()
    storage = None
    expired = 0

    for report in CampaignReport.objects.filter(
        status=Status.COMPLETED, expires_at__lte=now
    ).iterator():
        if report.file_key:
            storage = storage or AzureBlobStorageService()
            try:
                storage.delete_blob_direct(report.file_key)
            except Exception:
                logger.exception("could not delete expired report file %s", report.file_key)
                continue

        expired += CampaignReport.objects.filter(
            pk=report.pk, status=Status.COMPLETED
        ).update(status=Status.EXPIRED, file_key="", updated_at=now)

    return expired


def fail_stalled_reports(now=None):
    """Fail reports that will otherwise never move. Returns a count.

    Two ways a report gets stuck, and nothing else notices either:

    * RUNNING, but its worker was killed mid-report.
    * PENDING, but its task was never delivered — a lost ``on_commit``, a broker outage.

    Measured from ``updated_at`` — the last progress write, claim, or hand-back to PENDING —
    so this is time *without progress*, not time since the report began. A large report
    still writing rows is left alone however long it has run, and one waiting out a
    retry's backoff is not taken for a lost task. The window must still exceed the longest
    silent stretch of a legitimate build: the overview aggregates, the final save, the
    upload.
    """
    now = now or timezone.now()
    cutoff = now - timedelta(minutes=stall_minutes())

    stalled = list(
        CampaignReport.objects.filter(
            status__in=CampaignReport.OPEN_STATUSES, updated_at__lt=cutoff
        ).values_list("pk", flat=True)
    )
    for report_id in stalled:
        mark_failed(report_id, STALLED_MESSAGE)
    return len(stalled)
