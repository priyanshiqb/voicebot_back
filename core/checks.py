"""
Deploy-time validation for settings whose failure mode is silent.

``manage.py check`` runs in CI and before every ``migrate``, which makes it the right
place to catch configuration that would otherwise only show up as a 500 on the first
request — or worse, as a security control that quietly did nothing.
"""

from django.conf import settings
from django.core.checks import Error, Warning, register

#: Kept as literals rather than imported from core.services.uploads: system checks run
#: during app loading, and importing a module that pulls in models at that point is how
#: AppRegistryNotReady happens.
UPLOAD_METHODS = ("PUT",)


@register()
def check_upload_presign_method(app_configs, **kwargs):
    """``UPLOAD_PRESIGN_METHOD`` must name a method the upload flows implement.

    A typo here is not cosmetic. The service raises ImproperlyConfigured rather than
    guessing, so an unrecognised value takes every upload endpoint down — and it would
    do so at the first upload rather than at deploy, which is the wrong end of the day
    to find out.
    """
    errors = []
    raw = getattr(settings, "UPLOAD_PRESIGN_METHOD", "PUT")
    method = str(raw).strip().upper()

    if method not in UPLOAD_METHODS:
        hint = f"Set it to one of: {', '.join(UPLOAD_METHODS)}."
        if method == "POST":
            hint = (
                "POST was an S3 presigned-POST policy, whose signed "
                "`content-length-range` let the object store reject an oversize body "
                "mid-upload. Azure Blob has no equivalent at any API version, so the "
                "option is gone rather than unimplemented. Set UPLOAD_PRESIGN_METHOD=PUT; "
                "the size ceiling is enforced at confirm-upload, which measures the "
                "object and deletes it if it is over."
            )
        errors.append(
            Error(
                f"UPLOAD_PRESIGN_METHOD is {raw!r}, which is not a supported upload "
                f"method.",
                hint=hint,
                id="core.E001",
            )
        )

    return errors


@register()
def check_object_storage_configured(app_configs, **kwargs):
    """Outside DEBUG, uploads must actually reach Azure.

    ``USE_AZURE_STORAGE`` is derived all-or-nothing from three settings, and a missing
    one falls back to ``FileSystemStorage`` without complaint. That fallback is correct
    for local work and catastrophic in a container: every upload lands on the
    container's own disk, survives until the next restart, and the loss is discovered
    later as a file that 404s with no trace of where it went.

    A warning rather than an error so a deliberately storage-less environment — a
    migration check, a smoke test — is not blocked by it.
    """
    errors = []

    if getattr(settings, "DEBUG", False):
        return errors

    if not getattr(settings, "USE_AZURE_STORAGE", False):
        missing = [
            name
            for name in ("AZURE_ACCOUNT_NAME", "AZURE_ACCOUNT_KEY", "AZURE_CONTAINER")
            if not getattr(settings, name, None)
        ]
        errors.append(
            Warning(
                "Object storage is not configured, so uploads will be written to local "
                "disk instead of Azure Blob.",
                hint=(
                    f"Unset: {', '.join(missing)}. Until all three are set, "
                    f"STORAGES['default'] falls back to FileSystemStorage and every "
                    f"uploaded file is lost when the process restarts."
                ),
                id="core.W002",
            )
        )

    return errors


@register()
def check_upload_url_ttl(app_configs, **kwargs):
    """The upload ticket must expire well inside the abandoned-upload sweep window.

    If the sweep cutoff were shorter than the ticket lifetime it would delete rows whose
    upload is still legitimately in flight, and the client would confirm into nothing.
    """
    errors = []
    ttl_minutes = int(getattr(settings, "UPLOAD_URL_TTL_MINUTES", 15))
    sweep_hours = int(getattr(settings, "PENDING_UPLOAD_TTL_HOURS", 24))

    if ttl_minutes <= 0:
        errors.append(
            Error(
                f"UPLOAD_URL_TTL_MINUTES is {ttl_minutes}, so every upload URL is "
                f"already expired when it is issued.",
                id="core.E002",
            )
        )
    elif ttl_minutes >= sweep_hours * 60:
        errors.append(
            Error(
                f"UPLOAD_URL_TTL_MINUTES ({ttl_minutes}m) is not shorter than "
                f"PENDING_UPLOAD_TTL_HOURS ({sweep_hours}h). The sweep would delete "
                f"uploads that are still in flight.",
                hint="Raise PENDING_UPLOAD_TTL_HOURS or lower UPLOAD_URL_TTL_MINUTES.",
                id="core.E003",
            )
        )

    return errors
