from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from tenant.common.models.tenant_base_model_core import TenantBaseModelCore


def contact_import_upload_path(instance, filename):
    return f"t-{instance.tenant_id}/contact_imports/{instance.id}_{filename}"


def default_import_expiry():
    hours = getattr(settings, "CONTACT_IMPORT_SESSION_TTL_HOURS", 24)
    return timezone.now() + timezone.timedelta(hours=hours)


class ContactImportFile(TenantBaseModelCore):
    """
    An uploaded CSV awaiting or backing an import session.

    The row is created when the presigned URL is issued, so the object key lives
    server-side and is never accepted back from the client. That is what keeps a caller
    from confirming — and then importing — another tenant's file.
    """

    class UploadStatus(models.TextChoices):
        PENDING = "PENDING", "Awaiting upload"
        CONFIRMED = "CONFIRMED", "Upload confirmed"
        FAILED = "FAILED", "Upload failed"

    file = models.FileField(
        upload_to=contact_import_upload_path,
        max_length=500,
        help_text=_("The CSV object in the storage bucket."),
    )
    original_name = models.CharField(max_length=255, blank=True)
    extension = models.CharField(max_length=10, blank=True)
    file_size_in_bytes = models.BigIntegerField(default=0)
    upload_status = models.CharField(
        max_length=10,
        choices=UploadStatus.choices,
        default=UploadStatus.PENDING,
        db_index=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contact_import_files_created",
    )

    class Meta:
        db_table = "core_contact_import_file"
        verbose_name = "Contact Import File"
        verbose_name_plural = "Contact Import Files"
        ordering = ["-created_at"]

    def __str__(self):
        return self.original_name or str(self.id)


class ContactImportSession(TenantBaseModelCore):
    """
    One staged import: parse, review, resolve, commit.

    Sessions are tenant-wide — any colleague can review and commit one — so every
    state transition is attributed and guarded. Surviving as an audit record after
    commit is deliberate: "who imported these 4,000 contacts, and what was skipped"
    is unanswerable once the staging rows are gone.
    """

    class Status(models.TextChoices):
        UPLOADED = "UPLOADED", "Uploaded"
        PARSING = "PARSING", "Parsing"
        READY = "READY", "Ready for review"
        COMMITTING = "COMMITTING", "Committing"
        COMPLETED = "COMPLETED", "Completed"
        PARTIAL = "PARTIAL", "Completed with failures"
        FAILED = "FAILED", "Failed"
        DISCARDED = "DISCARDED", "Discarded"
        EXPIRED = "EXPIRED", "Expired"

    class ConflictStrategy(models.TextChoices):
        SKIP = "SKIP", "Skip conflicting rows"
        UPDATE = "UPDATE", "Update the existing contact"
        FAIL = "FAIL", "Refuse to commit while conflicts remain"

    # States in which the staging rows still exist and may be edited.
    OPEN_STATUSES = [Status.UPLOADED, Status.PARSING, Status.READY]
    # States from which no further work happens.
    TERMINAL_STATUSES = [
        Status.COMPLETED,
        Status.PARTIAL,
        Status.FAILED,
        Status.DISCARDED,
        Status.EXPIRED,
    ]

    import_file = models.ForeignKey(
        ContactImportFile,
        on_delete=models.PROTECT,
        related_name="sessions",
    )
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.UPLOADED,
        db_index=True,
    )
    default_region = models.CharField(
        max_length=2,
        blank=True,
        help_text=_("ISO-3166 alpha-2 used to parse numbers not in international form."),
    )
    conflict_strategy = models.CharField(
        max_length=10,
        choices=ConflictStrategy.choices,
        default=ConflictStrategy.SKIP,
        help_text=_("Default for conflicting rows. SKIP so nothing is overwritten by accident."),
    )
    column_map = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Optional CSV header -> model field overrides."),
    )
    expires_at = models.DateTimeField(default=default_import_expiry, db_index=True)

    total_rows = models.IntegerField(default=0)
    valid_rows = models.IntegerField(default=0)
    conflict_rows = models.IntegerField(default=0)
    invalid_rows = models.IntegerField(default=0)

    created_count = models.IntegerField(default=0)
    updated_count = models.IntegerField(default=0)
    skipped_count = models.IntegerField(default=0)
    failed_count = models.IntegerField(default=0)

    error_message = models.TextField(blank=True)
    notices = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Non-fatal parse warnings, e.g. ignored consent columns."),
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contact_import_sessions_created",
    )
    committed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contact_import_sessions_committed",
    )
    discarded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contact_import_sessions_discarded",
    )
    committed_at = models.DateTimeField(null=True, blank=True)
    discarded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "core_contact_import_session"
        verbose_name = "Contact Import Session"
        verbose_name_plural = "Contact Import Sessions"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "status"]),
        ]

    def __str__(self):
        return f"{self.import_file_id} ({self.status})"

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at


class ContactImportRow(TenantBaseModelCore):
    """
    One CSV line, staged for review.

    `status` is always derived server-side from re-validation — accepting it from a
    client would let a row be marked resolved without the conflict actually being
    fixed, which surfaces as an IntegrityError at commit time.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Ready to create"
        INVALID = "INVALID", "Malformed row"
        CONFLICT_EXISTING = "CONFLICT_EXISTING", "Matches an existing contact"
        CONFLICT_DUPLICATE = "CONFLICT_DUPLICATE", "Duplicated within this file"
        CONFLICT_ARCHIVED = "CONFLICT_ARCHIVED", "Matches a deleted contact"
        RESOLVED = "RESOLVED", "Resolved"
        SKIPPED = "SKIPPED", "Skipped"
        COMMITTED = "COMMITTED", "Committed"
        FAILED = "FAILED", "Failed at commit"

    class Resolution(models.TextChoices):
        CREATE = "CREATE", "Create a new contact"
        UPDATE = "UPDATE", "Update the existing contact"
        SKIP = "SKIP", "Skip this row"

    CONFLICT_STATUSES = [
        Status.CONFLICT_EXISTING,
        Status.CONFLICT_DUPLICATE,
        Status.CONFLICT_ARCHIVED,
    ]
    # Rows the commit pass will act on.
    COMMITTABLE_STATUSES = [Status.PENDING, Status.RESOLVED]

    session = models.ForeignKey(
        ContactImportSession,
        on_delete=models.CASCADE,
        related_name="rows",
        db_index=True,
    )
    row_number = models.IntegerField(
        help_text=_("1-based line offset in the source CSV, for user-facing errors.")
    )
    raw_data = models.JSONField(
        default=dict, help_text=_("Verbatim cells as uploaded. Never mutated.")
    )
    parsed_data = models.JSONField(
        default=dict, help_text=_("Cleaned values. This is what gets committed.")
    )
    phone_e164 = models.CharField(max_length=20, blank=True, db_index=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    resolution = models.CharField(
        max_length=10, choices=Resolution.choices, blank=True
    )
    conflict_contact = models.ForeignKey(
        "core.Contact",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_conflicts",
    )
    error_message = models.TextField(blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contact_import_rows_updated",
        help_text=_("Last colleague to edit this row; sessions are tenant-wide."),
    )

    class Meta:
        db_table = "core_contact_import_row"
        verbose_name = "Contact Import Row"
        verbose_name_plural = "Contact Import Rows"
        ordering = ["row_number"]
        indexes = [
            models.Index(fields=["session", "status"]),
        ]

    def __str__(self):
        return f"row {self.row_number} ({self.status})"

    @property
    def is_conflict(self):
        return self.status in self.CONFLICT_STATUSES
