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
UPLOAD_METHODS = ("POST", "PUT")


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
        errors.append(
            Error(
                f"UPLOAD_PRESIGN_METHOD is {raw!r}, which is not a supported upload "
                f"method.",
                hint=f"Set it to one of: {', '.join(UPLOAD_METHODS)}.",
                id="core.E001",
            )
        )
        return errors

    if method == "PUT":
        errors.append(
            Warning(
                "Presigned uploads are using PUT, which cannot enforce a size limit.",
                hint=(
                    "A PUT URL issued for a 1 MB file will accept a 500 MB one; the "
                    "overage is only caught at confirm-upload, after the bytes have "
                    "been transferred and stored. Set UPLOAD_PRESIGN_METHOD=POST once "
                    "the frontend sends multipart form uploads and the object store is "
                    "confirmed to support POST Object."
                ),
                id="core.W001",
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
