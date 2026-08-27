"""
Staged contact import: CSV handling plus session orchestration.

The flow is parse -> review -> resolve -> commit. Parsing and committing run off the
request thread; the endpoints only make the state transitions that must be atomic.
"""

import csv
import io
import logging

from charset_normalizer import from_bytes
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models.contact import Contact
from core.models.contact_import import (
    ContactImportRow,
    ContactImportSession,
)
from utils.phone import PhoneNormalizationError, normalize_phone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Column contract
# ---------------------------------------------------------------------------

CANONICAL_COLUMNS = [
    "first_name",
    "last_name",
    "phone_number",
    "email_address",
    "company",
    "tags",
    "notes",
]

REQUIRED_COLUMNS = ["phone_number"]

# Tags are pipe-delimited: a comma would collide with the CSV delimiter itself.
TAG_DELIMITER = "|"

# Real uploads come out of Excel, Google Sheets, and half a dozen CRMs. Accepting only
# the canonical spelling would bounce most of them.
COLUMN_ALIASES = {
    "first_name": ["first name", "firstname", "first", "given name", "fname"],
    "last_name": ["last name", "lastname", "last", "surname", "family name", "lname"],
    "phone_number": [
        "phone", "phone number", "phonenumber", "mobile", "mobile number",
        "contact", "contact number", "telephone", "tel", "msisdn", "number",
    ],
    "email_address": ["email", "email address", "emailaddress", "e-mail", "mail"],
    "company": [
        "organisation", "organization", "org", "account", "business", "company name",
    ],
    "tags": ["tag", "labels", "label", "segments", "segment"],
    "notes": ["note", "comment", "comments", "description", "remarks"],
}

# Consent and suppression are never writable by import: re-uploading a stale list must
# not resurrect someone who opted out. Present columns are dropped with a notice.
IGNORED_COLUMNS = {
    "consent_status", "consent status", "consent", "consent_source", "consent_at",
    "do_not_call", "do not call", "dnc", "opted_out_at", "opt_out", "opted out",
}

TEMPLATE_EXAMPLE_ROW = {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "phone_number": "+14155552671",
    "email_address": "ada@example.com",
    "company": "Analytical Engines",
    "tags": "vip|lead",
    "notes": "Met at the London conference",
}

# Fields an UPDATE resolution is allowed to touch. Consent and suppression are
# excluded on purpose — see IGNORED_COLUMNS.
UPDATABLE_FIELDS = [
    "first_name",
    "last_name",
    "phone_number",
    "email_address",
    "company",
    "tags",
    "notes",
]

_ZERO_WIDTH = ("﻿", "​")


def _normalize_header(value):
    """Lowercase, trim, and strip BOM/zero-width junk so header matching is forgiving."""
    if value is None:
        return ""
    cleaned = str(value)
    for junk in _ZERO_WIDTH:
        cleaned = cleaned.replace(junk, "")
    cleaned = cleaned.strip().lower()
    # Treat underscores and spaces as equivalent: "First Name" == "first_name".
    return " ".join(cleaned.replace("_", " ").split())


_ALIAS_LOOKUP = {}
for _canonical, _aliases in COLUMN_ALIASES.items():
    _ALIAS_LOOKUP[_normalize_header(_canonical)] = _canonical
    for _alias in _aliases:
        _ALIAS_LOOKUP[_normalize_header(_alias)] = _canonical

_IGNORED_LOOKUP = {_normalize_header(name) for name in IGNORED_COLUMNS}


class ImportError_(Exception):
    """A fatal problem with the file itself; the session cannot be reviewed."""


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------


def build_template_csv():
    """The downloadable CSV template: headers plus one example row."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CANONICAL_COLUMNS)
    writer.writerow([TEMPLATE_EXAMPLE_ROW[column] for column in CANONICAL_COLUMNS])
    return buffer.getvalue()


def decode_csv_bytes(raw):
    """
    Decode uploaded bytes to text.

    Excel writes UTF-8 with a BOM and plenty of exports are still cp1252, so try
    utf-8-sig first and fall back to detection.
    """
    if isinstance(raw, str):
        return raw

    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue

    match = from_bytes(raw).best()
    if match is None:
        raise ImportError_(
            "Could not read the file's text encoding. Save it as UTF-8 CSV and retry."
        )
    return str(match)


def sniff_delimiter(text, default=","):
    """Detect the delimiter. European Excel writes `;`; some exports use tabs."""
    sample = text[:8192]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return default


def resolve_headers(fieldnames, column_map=None):
    """
    Map CSV headers onto canonical fields.

    Returns (mapping, ignored, unknown). An explicit column_map wins over the alias
    table so the UI can always override a bad guess.
    """
    column_map = {_normalize_header(k): v for k, v in (column_map or {}).items()}

    mapping, ignored, unknown = {}, [], []

    for header in fieldnames or []:
        key = _normalize_header(header)
        if not key:
            continue

        if key in column_map:
            mapping[header] = column_map[key]
            continue

        if key in _IGNORED_LOOKUP:
            ignored.append(header)
            continue

        canonical = _ALIAS_LOOKUP.get(key)
        if canonical:
            # First header wins if a file has both "Phone" and "Mobile".
            if canonical not in mapping.values():
                mapping[header] = canonical
        else:
            unknown.append(header)

    return mapping, ignored, unknown


def missing_required_columns(mapping):
    mapped = set(mapping.values())
    return [column for column in REQUIRED_COLUMNS if column not in mapped]


def clean_cell(value):
    """Strip BOM/zero-width characters and surrounding whitespace from a cell."""
    if value is None:
        return ""
    cleaned = str(value)
    for junk in _ZERO_WIDTH:
        cleaned = cleaned.replace(junk, "")
    return cleaned.strip()


def parse_tags(value, max_tags=25):
    """Split the pipe-delimited tags cell, trimming, deduping, and clamping."""
    if isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raw = clean_cell(value)
        if not raw:
            return []
        items = raw.split(TAG_DELIMITER)

    tags = []
    for item in items:
        tag = str(item).strip()[:50]
        if tag and tag not in tags:
            tags.append(tag)
    return tags[:max_tags]


def prepare_reader(text, column_map=None):
    """Build a DictReader plus the resolved header mapping."""
    delimiter = sniff_delimiter(text)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    mapping, ignored, unknown = resolve_headers(reader.fieldnames, column_map)
    return reader, mapping, ignored, unknown


def iter_mapped_rows(reader, mapping):
    """
    Yield (row_number, raw_data, mapped_data) for each non-blank line.

    row_number is the 1-based line in the file including the header, so it lines up
    with what the user sees in their spreadsheet.
    """
    for row_number, raw_row in enumerate(reader, start=2):
        raw_data = {
            key: clean_cell(value)
            for key, value in raw_row.items()
            if key is not None
        }

        if not any(raw_data.values()):
            continue  # blank line

        mapped = {
            canonical: raw_data.get(header, "")
            for header, canonical in mapping.items()
        }
        yield row_number, raw_data, mapped


# ---------------------------------------------------------------------------
# Row validation
# ---------------------------------------------------------------------------


def build_row_payload(mapped, region, max_tags=None):
    """
    Clean one mapped row into contact fields.

    Returns (parsed_data, phone_e164, error_message). A non-None error means the row
    is INVALID — malformed input, which is a different problem from a conflict.
    """
    max_tags = max_tags or getattr(settings, "CONTACT_MAX_TAGS_PER_CONTACT", 25)

    parsed = {
        "first_name": clean_cell(mapped.get("first_name"))[:120],
        "last_name": clean_cell(mapped.get("last_name"))[:120],
        "phone_number": clean_cell(mapped.get("phone_number"))[:32],
        "email_address": clean_cell(mapped.get("email_address"))[:254],
        "company": clean_cell(mapped.get("company"))[:200],
        "tags": parse_tags(mapped.get("tags"), max_tags=max_tags),
        "notes": clean_cell(mapped.get("notes")),
    }

    email = parsed["email_address"]
    if email and ("@" not in email or email.startswith("@") or email.endswith("@")):
        return parsed, "", f"'{email}' is not a valid email address."

    try:
        e164, _region_code = normalize_phone(parsed["phone_number"], region=region)
    except PhoneNormalizationError as exc:
        return parsed, "", str(exc)

    return parsed, e164, None


def revalidate_row(row, save=True, actor=None):
    """
    Re-derive a row's status after an edit.

    This is the only path that sets `status`; the client never supplies it. A row is
    RESOLVED only when the edited data genuinely no longer conflicts, or when an
    explicit resolution has been chosen for a conflict that remains.
    """
    session = row.session
    parsed, e164, error = build_row_payload(row.parsed_data, session.default_region)

    row.parsed_data = parsed
    row.phone_e164 = e164
    row.conflict_contact = None
    row.error_message = error or ""

    if error:
        row.status = ContactImportRow.Status.INVALID
    else:
        existing = (
            Contact.objects.filter(tenant_id=session.tenant_id, phone_e164=e164)
            .only("id")
            .first()
        )
        duplicate_in_file = (
            ContactImportRow.all_objects.filter(session=session, phone_e164=e164)
            .exclude(pk=row.pk)
            .exclude(status=ContactImportRow.Status.SKIPPED)
            .exists()
        )

        if existing is not None:
            row.conflict_contact = existing
            row.status = ContactImportRow.Status.CONFLICT_EXISTING
            row.error_message = "A contact with this phone number already exists."
        elif duplicate_in_file:
            row.status = ContactImportRow.Status.CONFLICT_DUPLICATE
            row.error_message = "This phone number appears more than once in the file."
        else:
            archived = (
                Contact.all_objects.filter(
                    tenant_id=session.tenant_id, phone_e164=e164, is_deleted=True
                )
                .only("id")
                .first()
            )
            if archived is not None:
                row.conflict_contact = archived
                row.status = ContactImportRow.Status.CONFLICT_ARCHIVED
                row.error_message = (
                    "This phone number matches a deleted contact. "
                    "Creating will make a new contact."
                )
            else:
                row.status = ContactImportRow.Status.PENDING
                row.error_message = ""

    # An explicit choice settles a conflict that still stands.
    if row.resolution and row.status in ContactImportRow.CONFLICT_STATUSES:
        if row.resolution == ContactImportRow.Resolution.SKIP:
            row.status = ContactImportRow.Status.SKIPPED
        else:
            row.status = ContactImportRow.Status.RESOLVED

    if actor is not None:
        row.updated_by = actor

    if save:
        row.save(
            update_fields=[
                "parsed_data",
                "phone_e164",
                "status",
                "resolution",
                "conflict_contact",
                "error_message",
                "updated_by",
                "updated_at",
            ]
        )
    return row


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


def parse_session(session_id):
    """Read the CSV, stage every row, and flag conflicts. Runs in a Celery worker."""
    session = ContactImportSession.all_objects.filter(pk=session_id).first()
    if session is None:
        logger.warning("parse_session: session %s not found", session_id)
        return None

    if session.status != ContactImportSession.Status.UPLOADED:
        logger.info(
            "parse_session: session %s is %s, not UPLOADED — skipping",
            session_id,
            session.status,
        )
        return session

    session.status = ContactImportSession.Status.PARSING
    session.save(update_fields=["status", "updated_at"])

    try:
        _do_parse(session)
    except ImportError_ as exc:
        _fail_session(session, str(exc))
    except Exception as exc:  # noqa: BLE001 — a bad file must not wedge the worker
        logger.exception("parse_session failed for %s", session_id)
        _fail_session(session, f"Could not read the file: {exc}")

    return session


def _fail_session(session, message):
    session.status = ContactImportSession.Status.FAILED
    session.error_message = message[:2000]
    session.save(update_fields=["status", "error_message", "updated_at"])


def _do_parse(session):
    max_rows = getattr(settings, "CONTACT_IMPORT_MAX_ROWS", 50_000)
    max_tags = getattr(settings, "CONTACT_MAX_TAGS_PER_CONTACT", 25)

    import_file = session.import_file
    try:
        with import_file.file.open("rb") as handle:
            raw = handle.read()
    except Exception as exc:  # noqa: BLE001
        raise ImportError_(f"The uploaded file could not be retrieved: {exc}")

    text = decode_csv_bytes(raw)
    reader, mapping, ignored, unknown = prepare_reader(text, session.column_map)

    missing = missing_required_columns(mapping)
    if missing:
        raise ImportError_(
            f"The file is missing required column(s): {', '.join(missing)}. "
            "Download the template for the expected headers."
        )

    notices = []
    if ignored:
        notices.append(
            "Consent and do-not-call columns are ignored by import and were skipped: "
            + ", ".join(ignored)
        )
    if unknown:
        notices.append("Unrecognised column(s) ignored: " + ", ".join(unknown))

    rows = []
    seen_phones = set()
    counts = {"valid": 0, "conflict": 0, "invalid": 0}

    for row_number, raw_data, mapped in iter_mapped_rows(reader, mapping):
        if len(rows) >= max_rows:
            raise ImportError_(
                f"The file has more than {max_rows:,} rows. Split it and import in parts."
            )

        parsed, e164, error = build_row_payload(
            mapped, session.default_region, max_tags=max_tags
        )

        row = ContactImportRow(
            tenant_id=session.tenant_id,
            session=session,
            row_number=row_number,
            raw_data=raw_data,
            parsed_data=parsed,
            phone_e164=e164,
        )

        if error:
            row.status = ContactImportRow.Status.INVALID
            row.error_message = error
            counts["invalid"] += 1
        elif e164 in seen_phones:
            row.status = ContactImportRow.Status.CONFLICT_DUPLICATE
            row.error_message = "This phone number appears more than once in the file."
            counts["conflict"] += 1
        else:
            seen_phones.add(e164)
            row.status = ContactImportRow.Status.PENDING
            counts["valid"] += 1

        rows.append(row)

    # One pass for existing contacts, one for archived. Chunked so a large import does
    # not build a single enormous IN clause.
    live_phones = [r.phone_e164 for r in rows if r.status == ContactImportRow.Status.PENDING]
    existing = _lookup_contacts(session.tenant_id, live_phones, deleted=False)
    archived = _lookup_contacts(session.tenant_id, live_phones, deleted=True)

    for row in rows:
        if row.status != ContactImportRow.Status.PENDING:
            continue
        match = existing.get(row.phone_e164)
        if match is not None:
            row.status = ContactImportRow.Status.CONFLICT_EXISTING
            row.conflict_contact_id = match
            row.error_message = "A contact with this phone number already exists."
            counts["valid"] -= 1
            counts["conflict"] += 1
            continue

        archived_match = archived.get(row.phone_e164)
        if archived_match is not None:
            row.status = ContactImportRow.Status.CONFLICT_ARCHIVED
            row.conflict_contact_id = archived_match
            row.error_message = (
                "This phone number matches a deleted contact. "
                "Creating will make a new contact."
            )
            counts["valid"] -= 1
            counts["conflict"] += 1

    ContactImportRow.objects.bulk_create(rows, batch_size=1000)

    session.total_rows = len(rows)
    session.valid_rows = counts["valid"]
    session.conflict_rows = counts["conflict"]
    session.invalid_rows = counts["invalid"]
    session.notices = notices
    session.status = ContactImportSession.Status.READY
    session.error_message = ""
    session.save(
        update_fields=[
            "total_rows",
            "valid_rows",
            "conflict_rows",
            "invalid_rows",
            "notices",
            "status",
            "error_message",
            "updated_at",
        ]
    )


def _lookup_contacts(tenant_id, phones, deleted=False):
    """{phone_e164: contact_id} for the given numbers, chunked to bound the IN clause."""
    found = {}
    manager = Contact.all_objects if deleted else Contact.objects
    for start in range(0, len(phones), 1000):
        chunk = phones[start : start + 1000]
        if not chunk:
            continue
        qs = manager.filter(tenant_id=tenant_id, phone_e164__in=chunk)
        if deleted:
            qs = qs.filter(is_deleted=True)
        found.update(dict(qs.values_list("phone_e164", "id")))
    return found


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------


class CommitRejected(Exception):
    """The session is not in a state that can be committed."""


def begin_commit(session_id, actor=None, ignore_unresolved=False):
    """
    Claim a session for commit.

    Runs synchronously in the request so a double-clicked commit loses the race here
    rather than inserting everything twice. Sessions are tenant-wide, so this also
    covers two colleagues committing at the same moment.
    """
    with transaction.atomic():
        session = (
            ContactImportSession.all_objects.select_for_update()
            .filter(pk=session_id)
            .first()
        )
        if session is None:
            raise CommitRejected("Import session not found.")

        if session.status != ContactImportSession.Status.READY:
            raise CommitRejected(
                f"This import is {session.get_status_display().lower()} and cannot be committed."
            )

        if session.is_expired:
            raise CommitRejected("This import has expired. Start a new one.")

        unresolved = ContactImportRow.all_objects.filter(
            session=session, status__in=ContactImportRow.CONFLICT_STATUSES
        ).count()

        if unresolved and not ignore_unresolved:
            raise CommitRejected(
                f"{unresolved} row(s) still conflict. Resolve them, or commit with "
                "ignore_unresolved=true to skip them."
            )

        session.status = ContactImportSession.Status.COMMITTING
        session.committed_by = actor
        session.save(update_fields=["status", "committed_by", "updated_at"])

    return session


def commit_session(session_id):
    """Write the staged rows into Contact. Runs in a Celery worker."""
    session = ContactImportSession.all_objects.filter(pk=session_id).first()
    if session is None:
        logger.warning("commit_session: session %s not found", session_id)
        return None

    if session.status != ContactImportSession.Status.COMMITTING:
        logger.info(
            "commit_session: session %s is %s, not COMMITTING — skipping",
            session_id,
            session.status,
        )
        return session

    batch_size = getattr(settings, "CONTACT_IMPORT_COMMIT_BATCH_SIZE", 1000)
    totals = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}

    # Conflicting rows the user chose to ignore are recorded as skipped rather than
    # silently dropped, so the counts add up to the row total.
    skipped_conflicts = ContactImportRow.all_objects.filter(
        session=session, status__in=ContactImportRow.CONFLICT_STATUSES
    )
    totals["skipped"] += skipped_conflicts.count()
    skipped_conflicts.update(status=ContactImportRow.Status.SKIPPED)

    row_ids = list(
        ContactImportRow.all_objects.filter(
            session=session, status__in=ContactImportRow.COMMITTABLE_STATUSES
        )
        .order_by("row_number")
        .values_list("id", flat=True)
    )

    for start in range(0, len(row_ids), batch_size):
        chunk = row_ids[start : start + batch_size]
        rows = list(
            ContactImportRow.all_objects.filter(id__in=chunk).order_by("row_number")
        )
        try:
            with transaction.atomic():
                result = _commit_batch(session, rows)
        except IntegrityError:
            # A concurrent writer won inside this batch. The transaction is dead, so
            # retry each row on its own rather than losing the whole chunk.
            logger.info(
                "commit_session: batch conflict in session %s, retrying row by row",
                session_id,
            )
            result = _commit_rows_individually(session, rows)

        for key in totals:
            totals[key] += result.get(key, 0)

    session.created_count = totals["created"]
    session.updated_count = totals["updated"]
    session.skipped_count = totals["skipped"]
    session.failed_count = totals["failed"]
    session.committed_at = timezone.now()

    if totals["failed"]:
        # Keep the rows: a partial commit is only recoverable if you can see which
        # rows landed and which did not.
        session.status = ContactImportSession.Status.PARTIAL
        session.save(
            update_fields=[
                "created_count", "updated_count", "skipped_count", "failed_count",
                "committed_at", "status", "updated_at",
            ]
        )
    else:
        session.status = ContactImportSession.Status.COMPLETED
        session.save(
            update_fields=[
                "created_count", "updated_count", "skipped_count", "failed_count",
                "committed_at", "status", "updated_at",
            ]
        )
        purge_session_data(session)

    return session


def _build_contact(session, row):
    data = row.parsed_data or {}
    contact = Contact(
        tenant_id=session.tenant_id,
        first_name=data.get("first_name", ""),
        last_name=data.get("last_name", ""),
        phone_number=data.get("phone_number", ""),
        email_address=data.get("email_address", ""),
        company=data.get("company", ""),
        tags=data.get("tags", []) or [],
        notes=data.get("notes", ""),
        source=Contact.Source.IMPORT,
        # bulk_create bypasses save() and perform_create, so audit and identity
        # fields have to be set explicitly here.
        phone_e164=row.phone_e164,
        created_by=session.committed_by,
        updated_by=session.committed_by,
    )
    return contact


def _apply_update(contact, row, actor):
    """
    Merge an UPDATE row onto an existing contact.

    Blank incoming cells never overwrite populated fields — a CSV with an empty
    `company` column must not wipe every company value in the database.
    """
    data = row.parsed_data or {}
    for field in UPDATABLE_FIELDS:
        value = data.get(field)
        if value in (None, "", []):
            continue
        setattr(contact, field, value)

    contact.updated_by = actor
    return contact


def _commit_batch(session, rows):
    tenant_id = session.tenant_id
    actor = session.committed_by
    phones = [row.phone_e164 for row in rows if row.phone_e164]

    # Re-validate against live data: the parse-time flags are minutes to hours stale.
    live = {
        contact.phone_e164: contact
        for contact in Contact.objects.filter(
            tenant_id=tenant_id, phone_e164__in=phones
        )
    }

    to_create, to_update, touched = [], [], []
    counts = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}

    for row in rows:
        existing = live.get(row.phone_e164)

        if existing is None:
            to_create.append(_build_contact(session, row))
            row.status = ContactImportRow.Status.COMMITTED
            counts["created"] += 1
        elif row.resolution == ContactImportRow.Resolution.UPDATE:
            to_update.append(_apply_update(existing, row, actor))
            row.status = ContactImportRow.Status.COMMITTED
            counts["updated"] += 1
        else:
            row.status = ContactImportRow.Status.SKIPPED
            row.error_message = "A contact with this phone number already exists."
            counts["skipped"] += 1

        touched.append(row)

    if to_create:
        Contact.objects.bulk_create(to_create, batch_size=1000)
    if to_update:
        Contact.objects.bulk_update(
            to_update, UPDATABLE_FIELDS + ["updated_by"], batch_size=1000
        )

    ContactImportRow.all_objects.bulk_update(
        touched, ["status", "error_message"], batch_size=1000
    )
    return counts


def _commit_rows_individually(session, rows):
    """Fallback when a batch hits a concurrent writer — isolate the damage per row."""
    counts = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}
    actor = session.committed_by

    for row in rows:
        try:
            with transaction.atomic():
                existing = Contact.objects.filter(
                    tenant_id=session.tenant_id, phone_e164=row.phone_e164
                ).first()

                if existing is None:
                    contact = _build_contact(session, row)
                    # save() re-derives phone_e164, so it must use the session's region
                    # rather than the deployment default — otherwise a row parsed under
                    # one region would be stored under another.
                    contact.save(region=session.default_region)
                    row.status = ContactImportRow.Status.COMMITTED
                    counts["created"] += 1
                elif row.resolution == ContactImportRow.Resolution.UPDATE:
                    _apply_update(existing, row, actor).save(
                        region=session.default_region
                    )
                    row.status = ContactImportRow.Status.COMMITTED
                    counts["updated"] += 1
                else:
                    row.status = ContactImportRow.Status.SKIPPED
                    row.error_message = (
                        "A contact with this phone number already exists."
                    )
                    counts["skipped"] += 1
        except Exception as exc:  # noqa: BLE001 — one bad row must not stop the rest
            logger.warning("Contact import row %s failed: %s", row.pk, exc)
            row.status = ContactImportRow.Status.FAILED
            row.error_message = str(exc)[:1000]
            counts["failed"] += 1

        row.save(update_fields=["status", "error_message", "updated_at"])

    return counts


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


def _delete_object_quietly(object_key):
    """
    Best-effort blob removal.

    Never raises: a failed delete must not roll back the DB cleanup, or the sweep
    wedges on the same poison session every run.
    """
    if not object_key:
        return
    try:
        from utils.blob_storage.service import AzureBlobStorageService

        AzureBlobStorageService().delete_blob_direct(object_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not delete import object %s: %s", object_key, exc)


def purge_session_data(session):
    """
    Drop the staging rows and the uploaded blob, keeping the session as an audit record.

    Uses all_objects: the default manager returns a queryset whose delete() is blocked
    repo-wide in favour of soft deletion, and these rows are genuinely transient.
    """
    import_file = session.import_file
    object_key = None
    if import_file and import_file.file:
        object_key = import_file.file.name

    with transaction.atomic():
        ContactImportRow.all_objects.filter(session=session).delete()
        if object_key:
            transaction.on_commit(lambda: _delete_object_quietly(object_key))


def discard_session(session_id, actor=None):
    """Abandon a session at the user's request."""
    with transaction.atomic():
        session = (
            ContactImportSession.all_objects.select_for_update()
            .filter(pk=session_id)
            .first()
        )
        if session is None:
            raise CommitRejected("Import session not found.")

        if session.status in ContactImportSession.TERMINAL_STATUSES:
            raise CommitRejected(
                f"This import is already {session.get_status_display().lower()}."
            )

        session.status = ContactImportSession.Status.DISCARDED
        session.discarded_by = actor
        session.discarded_at = timezone.now()
        session.save(
            update_fields=["status", "discarded_by", "discarded_at", "updated_at"]
        )

    purge_session_data(session)
    return session


def cleanup_expired_sessions(now=None):
    """Beat task body: purge sessions whose TTL has passed."""
    now = now or timezone.now()
    stale_statuses = ContactImportSession.OPEN_STATUSES + [
        ContactImportSession.Status.COMMITTING
    ]

    sessions = ContactImportSession.all_objects.filter(
        expires_at__lte=now, status__in=stale_statuses
    )

    purged = 0
    for session in sessions:
        try:
            session.status = ContactImportSession.Status.EXPIRED
            session.error_message = "Expired before it was committed."
            session.save(update_fields=["status", "error_message", "updated_at"])
            purge_session_data(session)
            purged += 1
        except Exception:  # noqa: BLE001 — one bad session must not stop the sweep
            logger.exception("Failed to purge expired import session %s", session.pk)

    if purged:
        logger.info("Purged %s expired contact import session(s).", purged)
    return purged
