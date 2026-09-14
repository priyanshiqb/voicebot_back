"""One requested report on one campaign: the job record, and the file it produces.

The row is created by the request, before any work starts. That is what lets the API
answer "is it ready?" from the first millisecond, and what leaves evidence behind when a
worker dies part-way — a report that exists only inside a running task is a report that
silently never happened. See ``docs/feature-detail-scratch/campaign-report.md``.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _

from tenant.common.models.tenant_base_model import TenantBaseModel


class CampaignReport(TenantBaseModel):
    """A campaign report: requested, generated in the background, downloaded, expired.

    Audited, because ``created_by`` is who asked — and "who pulled this client's call
    data, and when" is a question worth being able to answer later.

    One row per request, never deduplicated. A user may want last week's report beside
    today's, and deciding whether a report requested two seconds ago is "the same" one
    is a question with no good answer.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Queued"
        RUNNING = "RUNNING", "Generating"
        COMPLETED = "COMPLETED", "Ready"
        FAILED = "FAILED", "Failed"
        EXPIRED = "EXPIRED", "Expired"

    class Format(models.TextChoices):
        XLSX = "XLSX", "Excel workbook"
        PDF = "PDF", "PDF document"

    #: Still in progress, and worth polling.
    OPEN_STATUSES = [Status.PENDING, Status.RUNNING]
    #: Nothing further will happen to these.
    TERMINAL_STATUSES = [Status.COMPLETED, Status.FAILED, Status.EXPIRED]

    campaign = models.ForeignKey(
        "core.Campaign",
        on_delete=models.PROTECT,
        related_name="reports",
        help_text=_(
            "PROTECT: removing a campaign from under its reports would leave files with "
            "nothing to say what they describe."
        ),
    )
    report_format = models.CharField(
        max_length=8,
        choices=Format.choices,
        default=Format.XLSX,
        help_text=_("One format per report. Asking for both is two requests and two rows."),
    )
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )

    file_key = models.CharField(
        max_length=500,
        blank=True,
        help_text=_(
            "The blob key. Blank until the upload succeeds, and never set on a failed "
            "report — the upload is the last step, so a job that fails leaves nothing "
            "downloadable behind."
        ),
    )
    file_name = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("What the download is called in the user's downloads folder."),
    )
    file_size_bytes = models.BigIntegerField(
        default=0,
        help_text=_("Shown before download, so nobody pulls 40 MB on a phone by surprise."),
    )

    total_rows = models.IntegerField(
        default=0,
        help_text=_("Members in the campaign when generation started."),
    )
    processed_rows = models.IntegerField(
        default=0,
        help_text=_(
            "Progress. Written every few hundred rows rather than every row — one "
            "UPDATE per member is a hundred thousand writes to move a progress bar."
        ),
    )
    rows_included = models.IntegerField(
        default=0,
        help_text=_(
            "Members actually written to the file. Below total_rows only for a PDF past "
            "its row cap; the workbook always carries everyone."
        ),
    )

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text=_(
            "Set on completion. After it the file is deleted and the row kept as "
            "EXPIRED, so 'this report existed' stays answerable without the storage bill."
        ),
    )
    error_message = models.TextField(
        blank=True,
        help_text=_("Why a FAILED report failed, in words a user can act on."),
    )

    class Meta:
        db_table = "core_campaign_report"
        verbose_name = "Campaign Report"
        verbose_name_plural = "Campaign Reports"
        ordering = ["-created_at"]
        indexes = [
            # The report list: this tenant's reports, optionally for one campaign.
            models.Index(
                fields=["tenant", "campaign", "-created_at"],
                name="core_report_campaign_idx",
            ),
            # Both sweeps: expired COMPLETED reports, and RUNNING ones that stalled.
            models.Index(
                fields=["status", "expires_at"],
                name="core_report_sweep_idx",
            ),
        ]

    def __str__(self):
        return f"{self.get_report_format_display()} report on {self.campaign_id} ({self.status})"

    @property
    def is_downloadable(self):
        return self.status == self.Status.COMPLETED and bool(self.file_key)

    @property
    def progress_percent(self):
        """0–100, never 100 until the file is actually written.

        Capped at 99 while running: the last rows are followed by an upload, and a bar
        that sits at 100% while nothing is downloadable reads as broken.
        """
        if self.status in (self.Status.COMPLETED, self.Status.EXPIRED):
            # EXPIRED finished too; only its file has gone. Left to the arithmetic below it
            # would read 99, because processed_rows equals total_rows and the cap applies.
            return 100
        if not self.total_rows:
            return 0
        return min(99, int(self.processed_rows * 100 / self.total_rows))
