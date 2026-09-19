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
import re
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from itertools import islice
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Avg, Count, Max, Q, Sum
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
from utils.querysets import batched_queryset

logger = logging.getLogger(__name__)

Status = CampaignReport.Status
Format = CampaignReport.Format

REPORT_FOLDER = "campaign-reports"

#: Members per page while streaming. Each page costs one query for the members and one or
#: two for their call data, so the page is small enough to keep those joins cheap and
#: large enough that the per-page cost disappears against the row count.
PAGE_SIZE = 500

#: Excel refuses a cell over 32,767 characters, and a long call's transcript passes that.
TRANSCRIPT_LIMIT = 32000

#: Dispositions listed in the overview. They are free text, so a runner that varies its
#: wording can produce hundreds; the tail is the long one nobody reads.
OVERVIEW_DISPOSITION_LIMIT = 15

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


def max_probing_questions():
    return max(0, int(getattr(settings, "CAMPAIGN_REPORT_MAX_PROBING_QUESTIONS", 25)))


def recording_link_hours():
    """How long a recording link inside a report stays valid: the report's own lifetime.

    Anything shorter and the links die while the workbook carrying them is still
    downloadable, which reads as a broken report.
    """
    return max(1, retention_days() * 24)


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


# --------------------------------------------------------------------------- #
# Probing questions
# --------------------------------------------------------------------------- #

#: Keys the runner writes into each ``AgentCallLog.probing_questions`` entry. Mirrored as
#: literals in the SQL below, which cannot take them as parameters without confusing
#: Postgres about the operand type of ``->>``.
QUESTION_KEY = "question"
POSITIVE_KEY = "isPositive"
SUMMARY_KEY = "answerSummary"

#: Long questions are cut in the header only. The answer cell keeps whatever was said.
PROBING_HEADER_LIMIT = 60

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class ProbingColumn:
    """One question, and the pair of member columns it produces.

    ``key`` matches a call's answers to this column; ``text`` is what the header shows;
    ``number`` keeps the header short and stable when the question is a paragraph long.
    """

    key: str
    text: str
    number: int


def _normalise_question(text):
    """A question's matching key: trimmed, inner whitespace collapsed, case-folded.

    Matching is by text because text is all the runner sends — there is no question id in
    the payload. Normalising is what stops "How do you manage leads?" and "how do you
    manage leads?" claiming two columns for one question.
    """
    if not text:
        return ""
    return _WHITESPACE.sub(" ", str(text)).strip().casefold()


def _logged_questions(campaign):
    """Every distinct probing question in this campaign's call logs, first seen first.

    One aggregate query rather than a scan in Python: the column is ``jsonb``, so Postgres
    unnests it. ``jsonb_typeof`` guards the row whose value is not an array — a single one
    of those would otherwise fail the whole query, and the payload is runner-written.
    """
    sql = """
        SELECT elem->>'question' AS question, MIN(log.created_at) AS first_seen
        FROM core_agent_call_log log
        JOIN core_campaign_member member ON member.id = log.campaign_member_id
        CROSS JOIN LATERAL jsonb_array_elements(log.probing_questions) elem
        WHERE member.campaign_id = %s
          AND log.is_deleted = false
          AND member.is_deleted = false
          AND jsonb_typeof(log.probing_questions) = 'array'
          AND COALESCE(elem->>'question', '') <> ''
        GROUP BY 1
        ORDER BY first_seen, question
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [str(campaign.pk)])
        return [row[0] for row in cursor.fetchall()]


def probing_columns_for(campaign):
    """The question columns for one campaign, in the order they will appear.

    Two sources, in this order:

    1. **The bot's configured questions**, by ``ProbingQuestion.priority`` — the set the
       agent was meant to ask. Taking these first means the columns are in a deliberate
       order, and a question nobody ever reached still gets a column, which is itself the
       answer: the conversation never got that far.
    2. **Anything else the call logs contain**, appended in first-seen order — a question
       edited on the bot after a call was made, or one asked ad hoc. Appending rather than
       dropping is why no answer goes missing from the file.

    Discovered **before** the member pass. The workbook is written append-only: once the
    header row is out, no column can be added. Two queries per report, none per row.
    """
    texts = []
    if campaign.agent_id:
        texts.extend(
            campaign.agent.probing_questions.order_by("priority", "created_at")
            .values_list("question", flat=True)
        )
    texts.extend(_logged_questions(campaign))

    unique = {}
    for text in texts:
        key = _normalise_question(text)
        if key and key not in unique:
            unique[key] = str(text).strip()

    limit = max_probing_questions()
    # Zero is "turn the columns off", a deliberate setting rather than a campaign that
    # outgrew the ceiling, so it is not worth a warning.
    if limit and len(unique) > limit:
        logger.warning(
            "campaign %s has %s probing questions; the report lists the first %s",
            campaign.pk, len(unique), limit,
        )

    return [
        ProbingColumn(key=key, text=text, number=number)
        for number, (key, text) in enumerate(islice(unique.items(), limit), start=1)
    ]


def probing_member_columns(probing):
    """Two columns per question: the answer summary, then whether it was positive."""
    columns = []
    for question in probing:
        label = question.text
        if len(label) > PROBING_HEADER_LIMIT:
            label = label[: PROBING_HEADER_LIMIT - 1].rstrip() + "…"
        columns.append(
            Column(f"probing_{question.number}_answer", f"Q{question.number} — {label}", 40)
        )
        columns.append(
            Column(f"probing_{question.number}_positive", f"Q{question.number} positive?", 14)
        )
    return columns


def _probing_answers(raw):
    """A call's probing payload as ``{normalised question: entry}``.

    Deliberately defensive: this is runner-written JSON, and a report must not fail
    because one call recorded a string where a list belongs. First entry wins when a
    question repeats within a call.
    """
    answers = {}
    if not isinstance(raw, list):
        return answers
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        key = _normalise_question(entry.get(QUESTION_KEY))
        if key and key not in answers:
            answers[key] = entry
    return answers


def _probing_values(raw, probing):
    """The two values per question column, in column order.

    Both blank when the call did not carry that question at all: "not asked" and "asked
    but unanswered" are different answers, and only the first should be silent.

    ``isPositive`` is written as sent, not reinterpreted. The runner writes ``false`` both
    for a negative answer and for a question that went unanswered, so the summary text is
    the only thing separating them — guessing here would invent a distinction the payload
    does not carry.
    """
    answers = _probing_answers(raw)
    values = []
    for question in probing:
        entry = answers.get(question.key)
        if entry is None:
            values.extend(("", ""))
            continue
        values.append(entry.get(SUMMARY_KEY) or "")
        positive = entry.get(POSITIVE_KEY)
        values.append("" if positive is None else ("Yes" if positive else "No"))
    return values


# --------------------------------------------------------------------------- #
# The call's own record
# --------------------------------------------------------------------------- #

def call_detail_columns(zone_name):
    """Everything the call log holds about the conversation. Workbook only.

    These are the columns a standalone call-analytics export carries and this report did
    not: who dialled whom, when it started and ended, how it went, and the artefacts —
    recording, summary, transcript.
    """
    return [
        Column("call_from", "Call from", 18),
        Column("call_to", "Call to", 18),
        Column("bot", "Bot", 20),
        Column("call_started_at", f"Call started at ({zone_name})", 24, DATETIME),
        Column("call_ended_at", f"Call ended at ({zone_name})", 24, DATETIME),
        Column("attended_by", "Attended by", 14),
        Column("mood", "Sentiment", 16),
        Column("mood_confidence", "Sentiment confidence", 18, NUMBER),
        Column("language", "Detected language", 18),
        Column("demo_at", f"Appointment booked for ({zone_name})", 26, DATETIME),
        Column("follow_up", "Follow-up note", 34),
        Column("recording", "Recording", 60),
        Column("summary", "AI summary", 70),
        Column("transcript", "Transcript", 80),
        Column("logged_at", f"Call logged at ({zone_name})", 24, DATETIME),
    ]


def _attended_by(log):
    """Who finished the call. Derived, because nothing records it directly.

    A transfer note is written only when the bot handed over, so its presence is the
    signal. Blank when there was no call at all — "nobody" is not "AI".
    """
    if log is None:
        return ""
    return "Human" if (log.human_transfer_detail or "").strip() else "AI"


def _transcript_text(raw):
    """The transcript as ``speaker: text`` lines, cut to what one cell can hold.

    Read defensively: this is runner-written JSON whose entries have carried different
    keys over time, and a report must not fail over one odd turn. The tail is dropped
    rather than the cell rejected — Excel refuses anything past its limit outright.
    """
    if not isinstance(raw, list):
        return ""

    lines = []
    for turn in raw:
        if not isinstance(turn, dict):
            lines.append(str(turn))
            continue
        speaker = str(turn.get("speaker") or turn.get("role") or "").strip()
        text = str(turn.get("text") or turn.get("content") or "").strip()
        if not text:
            continue
        lines.append(f"{speaker}: {text}" if speaker else text)

    joined = "\n".join(lines)
    if len(joined) > TRANSCRIPT_LIMIT:
        return joined[: TRANSCRIPT_LIMIT - 1].rstrip() + "…"
    return joined


def _storage_factory(storage=None):
    """One storage client per report, built only when a recording link is actually needed.

    Signing is local crypto with no network call, but constructing the client reads
    settings and opens a service client — not worth doing for a campaign whose calls were
    never recorded.
    """
    holder = {"service": storage}

    def get_storage():
        if holder["service"] is None:
            holder["service"] = AzureBlobStorageService()
        return holder["service"]

    return get_storage


def _recording_link(log, get_storage, hours):
    """A link to the recording: the runner's own URL when it sent one, else our copy signed.

    Signed for as long as the report itself lives, so links do not die while the workbook
    carrying them is still downloadable. **That makes a downloaded workbook a bearer of
    recording links for its retention window** — which is why reports are tenant-scoped,
    expire, and record who asked for them.
    """
    if log is None:
        return ""
    if log.external_recording_url:
        return log.external_recording_url

    name = getattr(getattr(log.recording_file, "file", None), "name", "")
    if not name:
        return ""
    try:
        return get_storage().get_presigned_url_for_object(name, expiry_hours=hours)
    except Exception:
        logger.warning(
            "could not sign a recording link for call %s", log.pk, exc_info=True
        )
        return ""


def _detail_values(member, log, demos, zone, get_storage, hours):
    """The call-detail values for one member, in ``call_detail_columns`` order."""
    return [
        log.caller_number if log else "",
        (log.callee_number or log.customer_number) if log else "",
        log.agent.bot_name if log and log.agent_id else "",
        _local(log.started_at, zone) if log else None,
        _local(log.ended_at, zone) if log else None,
        _attended_by(log),
        log.mood if log else "",
        log.mood_confidence if log else None,
        log.preferred_language if log else "",
        _local(demos.get(member.contact_id), zone),
        log.follow_up_detail if log else "",
        _recording_link(log, get_storage, hours),
        log.ai_summary if log else "",
        _transcript_text(log.transcript) if log else "",
        _local(log.created_at, zone) if log else None,
    ]


def member_columns(zone_name, probing=(), detailed=False):
    """The member sheet, in order. The time column's header names its zone.

    Three bands, and the order matters: the fixed columns first, so a client reading column
    K keeps reading the same thing; then the call's own record; then two columns per
    probing question, the band that varies from campaign to campaign.
    """
    columns = [
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
        Column("probing_count", "Probing questions asked", 16, NUMBER),
    ]
    if detailed:
        columns += call_detail_columns(zone_name)
    return columns + probing_member_columns(probing)


def member_queryset(campaign):
    """Every member of a campaign, in the order the report lists them.

    Call figures are **not** annotated here. They were one correlated subquery per column,
    which is fine for three; the detailed workbook needs fifteen, and fifteen correlated
    lookups per member across a six-figure audience is a different proposition.
    ``member_rows`` joins each page's call logs in one query instead.

    Same order as the members endpoint's default plus its tiebreaker, so a report and the
    screen it was requested from list people identically.
    """
    return (
        CampaignMember.objects.filter(campaign=campaign)
        .select_related("contact")
        .order_by("created_at", "id")
    )


def _pages(rows, size):
    """Chunk an iterator into lists, so each page's extras can be fetched in one query."""
    rows = iter(rows)
    while True:
        page = list(islice(rows, size))
        if not page:
            return
        yield page


def _latest_logs(members):
    """``{member id: newest AgentCallLog}`` for one page, in a single query.

    A member normally has one call; nothing enforces it, and a retried runner can post
    several. Newest wins, which is what the rest of the row describes too.
    """
    ids = [member.pk for member in members]
    if not ids:
        return {}

    logs = {}
    for log in (
        AgentCallLog.objects.filter(campaign_member_id__in=ids)
        .select_related("agent", "recording_file")
        .order_by("campaign_member_id", "-created_at")
    ):
        logs.setdefault(log.campaign_member_id, log)
    return logs


def _demo_times(campaign, members):
    """``{contact id: earliest appointment}`` booked since this campaign started.

    A booking is tied to a person through ``BookingParticipant.contact``, never to a call,
    so this answers "did an appointment appear for this person after the campaign began" —
    reportable, but not proof the bot booked it. Anything stronger needs the runner to
    report the booking it made.
    """
    from core.models.booking import ACTIVE_BOOKING_STATUSES
    from core.models.booking_participant import BookingParticipant

    contact_ids = [member.contact_id for member in members if member.contact_id]
    if not contact_ids:
        return {}

    since = campaign.started_at or campaign.created_at
    demos = {}
    rows = BookingParticipant.objects.filter(
        contact_id__in=contact_ids,
        booking__status__in=ACTIVE_BOOKING_STATUSES,
        booking__is_deleted=False,
        booking__created_at__gte=since,
    ).values_list("contact_id", "booking__start_time")

    for contact_id, start_time in rows:
        if start_time is None:
            continue
        earliest = demos.get(contact_id)
        if earliest is None or start_time < earliest:
            demos[contact_id] = start_time
    return demos


def _demo_count(campaign):
    """How many of this campaign's people have an appointment booked since it started.

    Distinct people, not bookings: someone who rebooked twice is one result, which is what
    "appointments booked" means to whoever reads it.
    """
    from core.models.booking import ACTIVE_BOOKING_STATUSES
    from core.models.booking_participant import BookingParticipant

    since = campaign.started_at or campaign.created_at
    return (
        BookingParticipant.objects.filter(
            contact__campaign_memberships__campaign=campaign,
            booking__status__in=ACTIVE_BOOKING_STATUSES,
            booking__is_deleted=False,
            booking__created_at__gte=since,
        )
        .values("contact_id")
        .distinct()
        .count()
    )


def _status_label(code):
    """``IN_PROGRESS`` as ``In progress``.

    Not the models' choice labels: those are written for operators ("Runner reported a
    finished call", "Handing off to runner"), and a report is read by the tenant's own
    client, who has never heard of a runner.
    """
    return str(code or "").replace("_", " ").capitalize()


def member_rows(campaign, zone, limit=None, probing=(), detailed=False, storage=None):
    """One tuple per member, streamed. Suppressed members included, deliberately.

    Everywhere else in the campaign module suppressed people are hidden — the runner is
    never told they exist. A tenant asking what happened to their campaign is owed "these
    people were not called, and here is why", which is the point of recording a reason.

    ``probing`` must be the same list the columns were built from, and ``detailed`` the
    same flag, or values land under the wrong headers.

    Paged on (created_at, id), the order ``member_queryset`` sorts by. Not ``.iterator()``:
    its named cursor does not survive pgbouncer's transaction pooling. Each page costs one
    query for the members and one for their call logs, plus one for appointments when the
    detailed columns are on.
    """
    page_size = PAGE_SIZE if limit is None else max(1, min(PAGE_SIZE, limit))
    members = batched_queryset(
        member_queryset(campaign), batch_size=page_size, order_by=("created_at", "id")
    )
    if limit is not None:
        members = islice(members, limit)

    get_storage = _storage_factory(storage)
    link_hours = recording_link_hours()

    for page in _pages(members, page_size):
        logs = _latest_logs(page)
        demos = _demo_times(campaign, page) if detailed else {}

        for member in page:
            log = logs.get(member.pk)
            contact = member.contact
            row = [
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
                log.duration_seconds if log else None,
                log.credits_charged if log else None,
                member.error_message,
                log.provider_call_id if log else "",
                log.probing_questions_count if log else None,
            ]
            if detailed:
                row += _detail_values(member, log, demos, zone, get_storage, link_hours)
            if probing:
                # From the same latest call as the figures above, so one row never mixes
                # answers from one attempt with a duration from another.
                row += _probing_values(log.probing_questions if log else None, probing)
            yield tuple(row)



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
    was_transferred = Q(human_transfer_detail__isnull=False) & ~Q(human_transfer_detail="")
    stats = calls.aggregate(
        total=Count("id"),
        connected=Count("id", filter=Q(duration_seconds__gt=0)),
        not_picked=Count("id", filter=Q(duration_seconds=0)),
        longest=Max("duration_seconds"),
        talk=Sum("duration_seconds"),
        average=Avg("duration_seconds", filter=Q(duration_seconds__gt=0)),
        credits=Sum("credits_charged"),
        charged=Count("id", filter=Q(credits_charged__isnull=False)),
        transferred=Count("id", filter=was_transferred),
        under_30=Count("id", filter=Q(duration_seconds__gt=0, duration_seconds__lt=30)),
        under_60=Count("id", filter=Q(duration_seconds__gte=30, duration_seconds__lt=60)),
        under_120=Count("id", filter=Q(duration_seconds__gte=60, duration_seconds__lt=120)),
        over_120=Count("id", filter=Q(duration_seconds__gte=120)),
    )
    attempts = members.aggregate(total=Sum("attempt_count"))["total"] or 0
    moods = list(
        calls.exclude(mood__isnull=True)
        .exclude(mood="")
        .values_list("mood")
        .annotate(n=Count("id"))
        .order_by("-n", "mood")
    )
    dispositions = list(
        calls.exclude(outcome__isnull=True)
        .exclude(outcome="")
        .values_list("outcome")
        .annotate(n=Count("id"))
        .order_by("-n", "outcome")[:OVERVIEW_DISPOSITION_LIMIT]
    )
    demos = _demo_count(campaign)
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
            ("Direction", "Outbound"),
            ("Total calls", stats["total"]),
            ("Connected", stats["connected"]),
            ("Connect rate", _pct(stats["connected"], stats["total"])),
            ("Not picked up", stats["not_picked"]),
            ("Not picked up rate", _pct(stats["not_picked"], stats["total"])),
            ("Attempts reported", attempts),
            ("Total talk time", _duration(stats["talk"])),
            ("Average duration", _duration(stats["average"])),
            ("Longest call", _duration(stats["longest"])),
        ]),
        ("Call length", [
            ("Under 30 seconds", stats["under_30"]),
            ("30 to 60 seconds", stats["under_60"]),
            ("60 to 120 seconds", stats["under_120"]),
            ("Over 120 seconds", stats["over_120"]),
            ("Note", "Connected calls only — a call that never connected is counted under Not picked up."),
        ]),
        ("Attendance", [
            ("Handled by the bot", max(0, stats["connected"] - stats["transferred"])),
            ("Transferred to a human", stats["transferred"]),
            ("Appointments booked", demos),
            ("Note", "A transfer counts when the call recorded one. Appointments are bookings made for these people since the campaign started, not proof the bot booked them."),
        ]),
        ("Sentiment", moods or [("None recorded", 0)]),
        ("Dispositions", [
            *(dispositions or [("None recorded", 0)]),
            ("Note", "The runner's own labels, counted as sent. They are free text, so two spellings of one outcome count separately."),
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

    # Workbook only, both of them. The PDF is the summary format: its landscape table is
    # already at its width limit with the fixed columns, and fifteen call columns plus two
    # per probing question would make it unreadable rather than more informative.
    detailed = report.report_format == Format.XLSX
    probing = probing_columns_for(campaign) if detailed else ()

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
        columns=member_columns(zone_name, probing, detailed=detailed),
        rows=member_rows(
            campaign, zone, limit=limit, probing=probing, detailed=detailed
        ),
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

    expiring = CampaignReport.objects.filter(status=Status.COMPLETED, expires_at__lte=now)
    # Paged on the pk, so rows this loop moves to EXPIRED cannot shift the next page.
    for report in batched_queryset(expiring):
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
