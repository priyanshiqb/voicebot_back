"""
The two-step presigned upload flow: issuing tickets, and sweeping the abandoned ones.

Every flow here reserves a ``PENDING`` row first, so the object key can stay
server-side, then hands the client a short-lived ticket to upload straight to the
object store, then waits for a confirm.

Both halves of that live in this module because they are the same decision seen from
two ends. A ticket that bounds the upload size (``POST``) means the confirm step is
checking a limit that was already enforced; a ticket that cannot (``PUT``) means the
overage is discovered only after the bytes have been paid for. And a ticket that is
never redeemed is what the sweep exists to collect: the row is invisible to the API,
but the bytes it points at are not free, and nothing in the product will ever show
them to you.
"""

import logging

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from core.models.agent_call_recording import AgentCallRecording
from core.models.catalog_item_media import CatalogItemMedia
from core.models.contact_import import ContactImportFile
from core.models.knowledge_base import KBFiles
from utils.files import signatures
from utils.s3_storage.service import S3StorageService

logger = logging.getLogger(__name__)

# --- issuing upload tickets --------------------------------------------------

#: Browser form POST. The size ceiling is signed into the policy, so the object store
#: refuses an oversize body *during* the upload.
UPLOAD_METHOD_POST = "POST"

#: Single presigned PUT. Simpler for the client, but carries no enforceable size bound:
#: a URL issued for 1 MB will accept 500 MB, and the overage is only caught at confirm,
#: after it has been transferred and stored.
UPLOAD_METHOD_PUT = "PUT"

UPLOAD_METHODS = (UPLOAD_METHOD_POST, UPLOAD_METHOD_PUT)

DEFAULT_UPLOAD_METHOD = UPLOAD_METHOD_PUT
DEFAULT_UPLOAD_URL_TTL_MINUTES = 15


def upload_presign_method():
    """Which ticket style to issue. See ``UPLOAD_PRESIGN_METHOD`` in settings.

    Invalid values raise rather than falling back. A typo here silently downgrades a
    security control, and ``core.checks`` surfaces it at ``manage.py check`` time so it
    never reaches a request.
    """
    method = str(
        getattr(settings, "UPLOAD_PRESIGN_METHOD", DEFAULT_UPLOAD_METHOD)
    ).strip().upper()
    if method not in UPLOAD_METHODS:
        raise ImproperlyConfigured(
            f"UPLOAD_PRESIGN_METHOD must be one of {', '.join(UPLOAD_METHODS)}; "
            f"got {method!r}."
        )
    return method


def upload_url_ttl_minutes():
    return int(
        getattr(settings, "UPLOAD_URL_TTL_MINUTES", DEFAULT_UPLOAD_URL_TTL_MINUTES)
    )


def build_upload_ticket(object_key, content_type, max_bytes, min_bytes=1, storage=None):
    """Everything a client needs to upload one object, in one shape for every flow.

    ``max_bytes`` is the flow's own ceiling — 10 MB for catalog media, 50 MB for a call
    recording — and under ``POST`` it stops being advisory: it is a signed condition the
    object store enforces itself.

    Returns::

        {
          "upload_method": "POST" | "PUT",
          "upload_url": str,
          "upload_fields": dict,   # POST only; empty for PUT
          "expires_in_minutes": int,
          "max_bytes": int,
        }

    Under ``POST`` the client sends ``upload_fields`` as multipart form data **with the
    file part last**, and a success is ``204``. Under ``PUT`` it sends the body raw with
    a matching ``Content-Type`` header, and a success is ``200``. Raises on failure; the
    callers mark their reserved row FAILED and answer 503.
    """
    storage = storage or S3StorageService()
    method = upload_presign_method()
    ttl_minutes = upload_url_ttl_minutes()

    if method == UPLOAD_METHOD_POST:
        presigned = storage.get_presigned_post(
            object_key,
            content_type,
            max_bytes=max_bytes,
            min_bytes=min_bytes,
            expiry_minutes=ttl_minutes,
        )
        upload_url = presigned["url"]
        upload_fields = presigned["fields"]
    else:
        upload_url = storage.get_presigned_put_url(
            object_key, content_type, max_bytes, expiry_minutes=ttl_minutes
        )
        upload_fields = {}

    return {
        "upload_method": method,
        "upload_url": upload_url,
        "upload_fields": upload_fields,
        "expires_in_minutes": ttl_minutes,
        "max_bytes": max_bytes,
    }


# --- verifying what actually arrived ------------------------------------------


def content_verification_enabled():
    return bool(getattr(settings, "UPLOAD_VERIFY_CONTENT", True))


def content_mismatch(storage, object_key, extension):
    """``(is_mismatch, detected)`` for a stored object against its declared extension.

    Reads the first few dozen bytes with a ranged GET — identifying a 50 MB recording
    costs 64 bytes, which is what makes this affordable on every confirm.

    Only a **positive** contradiction counts. A file we cannot identify, or an extension
    with no signature to check against (``.txt``, ``.csv``), returns ``(False, …)``:
    being unable to recognise something is not evidence against it, and a strict
    allowlist here would reject legitimate uploads for no security gain.

    Storage errors propagate. The caller decides, and the upload flows fail closed —
    otherwise "make the ranged GET fail" is the way around the check.
    """
    if not content_verification_enabled():
        return False, None

    head = storage.get_object_head_bytes(object_key, num_bytes=signatures.HEAD_BYTES)
    if not head:
        # Too short to carry a signature, or the backend does not do ranged reads.
        return False, None

    return signatures.contradicts_extension(head, extension)


# --- reconciling storage against the database ---------------------------------

#: Every model that stores an object under a tenant prefix. Wider than SWEPT_MODELS on
#: purpose: this is about which keys are *accounted for*, and a contact import file is
#: as accounted-for as anything else even though a different sweep owns its lifecycle.
KEY_OWNING_MODELS = [KBFiles, AgentCallRecording, CatalogItemMedia, ContactImportFile]


def known_object_keys(tenant_id):
    """Every object key the database still accounts for, for one tenant.

    ``all_objects`` throughout: a soft-deleted row is restorable and still owns its
    file, so treating it as unknown would delete the very thing a restore needs.
    """
    keys = set()
    for model in KEY_OWNING_MODELS:
        keys.update(
            name
            for name in model.all_objects.filter(tenant_id=tenant_id).values_list(
                "file", flat=True
            )
            if name
        )
    return keys


def find_orphaned_objects(tenant_id, older_than_hours=None, storage=None, now=None):
    """Objects in the bucket that no database row accounts for.

    The counterpart to :func:`sweep_pending_uploads`, and it sees what that one cannot.
    The sweep walks rows and deletes their objects; this walks *objects* and finds the
    ones with no row at all — left by a hard delete that predates the ``delete()``
    overrides, by a legacy flow, or by a request that wrote to storage and then failed
    before its row was saved. Nothing in the database points at them, so nothing else
    will ever find them.

    ``older_than_hours`` protects uploads still in flight: an object written seconds ago
    may belong to a row that is about to be created. Defaults to the same TTL the sweep
    uses.

    Read-only. Returns a list of ``{key, size, last_modified}``.
    """
    storage = storage or S3StorageService()
    now = now or timezone.now()
    if older_than_hours is None:
        older_than_hours = pending_upload_ttl_hours()
    cutoff = now - timezone.timedelta(hours=older_than_hours)

    accounted_for = known_object_keys(tenant_id)

    orphans = []
    for obj in storage.iter_objects(f"t-{tenant_id}/"):
        if obj["key"] in accounted_for:
            continue
        last_modified = obj.get("last_modified")
        # No timestamp means we cannot prove it is old enough to be safe to delete.
        if last_modified is None or last_modified > cutoff:
            continue
        orphans.append(obj)

    return orphans


# --- sweeping abandoned uploads ----------------------------------------------

#: Every model with a two-step presigned upload belongs here. One that is left out
#: leaks bytes silently — there is no error, just a bucket that grows.
#:
#: Contact import files are deliberately absent: a file there is owned by an import
#: session whose own TTL sweep purges it, and two sweeps racing on the same row is
#: worse than one.
SWEPT_MODELS = [
    (KBFiles, "Knowledge Base file"),
    (AgentCallRecording, "Call recording"),
    (CatalogItemMedia, "Catalog media"),
]

DEFAULT_TTL_HOURS = 24

#: How many ids a dry run reports per model before it stops listing.
SAMPLE_LIMIT = 20


def pending_upload_ttl_hours():
    return int(getattr(settings, "PENDING_UPLOAD_TTL_HOURS", DEFAULT_TTL_HOURS))


def sweep_pending_uploads(older_than_hours=None, tenant_id=None, dry_run=False):
    """Delete abandoned presigned uploads, rows and stored objects alike.

    Returns one result dict per model: ``{model, label, matched, deleted, failed,
    sample_ids}``. ``deleted`` is zero on a dry run, and ``matched`` is what would go.

    Only ``PENDING`` is swept. ``FAILED`` rows reached a terminal state with nothing
    behind them — confirm found no object, or presigning itself failed — so they cost
    a row and no storage.
    """
    if older_than_hours is None:
        older_than_hours = pending_upload_ttl_hours()

    cutoff = timezone.now() - timezone.timedelta(hours=older_than_hours)

    return [
        _sweep_one(model, label, cutoff, tenant_id, dry_run)
        for model, label in SWEPT_MODELS
    ]


def _sweep_one(model, label, cutoff, tenant_id, dry_run):
    result = {
        "model": model.__name__,
        "label": label,
        "matched": 0,
        "deleted": 0,
        "failed": 0,
        "sample_ids": [],
    }

    # all_objects, not objects: a PENDING row that was also soft-deleted still owns
    # its bytes, and the default manager would hide it from the sweep forever.
    queryset = model.all_objects.filter(
        upload_status=model.UploadStatus.PENDING, created_at__lte=cutoff
    )
    if tenant_id:
        queryset = queryset.filter(tenant_id=tenant_id)

    result["matched"] = queryset.count()
    if not result["matched"]:
        return result

    if dry_run:
        result["sample_ids"] = [
            str(record.id) for record in queryset[:SAMPLE_LIMIT]
        ]
        return result

    for record in queryset:
        try:
            # Row by row on purpose. Each model's delete() removes its stored object,
            # and a queryset bulk delete would both skip that and be refused outright
            # by BaseQuerySet.delete().
            record.delete()
            result["deleted"] += 1
        except Exception:  # noqa: BLE001 — one bad row must not stop the sweep
            result["failed"] += 1
            logger.exception("Could not delete pending %s %s", label, record.pk)

    if result["deleted"]:
        logger.info(
            "Swept %s abandoned %s upload(s).", result["deleted"], label.lower()
        )
    return result
