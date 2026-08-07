from django.conf import settings
from django.db import models

from utils.models.base_model_core import BaseModelCore


class EmailDeliveryLog(BaseModelCore):
    """
    Record of platform-sent email: signup verification, email change, password
    reset — everything sent on the platform's own credentials rather than a
    tenant's.

    **This is not the booking notification ledger.** ``core.NotificationLog``
    records tenant-to-customer mail and doubles as its idempotency mechanism: its
    ``dedup_key`` decides whether a booking notification re-sends, which is why
    deleting one of those rows *re-arms* it. Rows here have no such power —
    deleting one is simply forgetting. Keeping the two apart means a purge can
    never accidentally re-send a confirmation, and a resend button can never
    accidentally be blocked by a log row.

    It also could not live there structurally: ``NotificationLog`` requires a
    tenant, and signup and pre-tenant password reset happen while the sender is a
    ``TempUser`` and no tenant exists yet.

    Tier 1 (no audit FKs): rows are written by the system, never by a person, so
    "who created this" has no answer worth storing.

    ## Identifying the recipient across a signup

    Three columns can identify who a row is about, and which one is populated
    depends on how far along the person was:

    * ``user`` — an existing account. Password reset from the app.
    * ``temp_user_id`` — an in-progress signup, before any ``CustomUser`` exists.
    * ``recipient`` — always. The only one guaranteed to be set.

    ``temp_user_id`` is not redundant with ``recipient``. Changing the email
    mid-signup rewrites ``TempUser.email`` **in place**, so the messages before and
    after land on different addresses with nothing else in common. Since hijacking
    an in-progress signup by swapping its email is precisely the attack this log
    exists to make visible, a trail that splits in half at that moment would be
    worse than useless. The ``TempUser`` id survives the change and stitches it
    back together.

    Neither ``temp_user_id`` nor ``tenant_id`` is a ForeignKey, deliberately.
    ``TempUser`` is soft-deleted on promotion and tenants can be removed outright;
    an audit trail has to outlive the thing it describes, and "the record vanished
    when the subject did" is the failure mode that makes a log worthless in exactly
    the investigation it was kept for. Storing the bare id also keeps the ``user``
    app from taking on new cross-app coupling.

    ## What must never be stored here

    **The OTP code and the reset token.** ``utils.tasks.send_email_task`` takes
    ``otp_code`` as a parameter, so the temptation sits in the signature — but a
    table of live one-time codes is a credential store, and a worse liability than
    the visibility gap it would close. Purpose, recipient, outcome, and who asked.
    Nothing more. Subjects and bodies are excluded for the same reason: they add
    nothing ``purpose`` does not already say.
    """

    class Purpose(models.TextChoices):
        SIGNUP_OTP = "SIGNUP_OTP", "Signup verification OTP"
        EMAIL_CHANGE = "EMAIL_CHANGE", "Email change verification"
        PASSWORD_RESET = "PASSWORD_RESET", "Password reset OTP"
        SMTP_TEST = "SMTP_TEST", "Tenant SMTP test message"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SENT = "SENT", "Sent"
        FAILED = "FAILED", "Failed"

    purpose = models.CharField(
        max_length=32,
        choices=Purpose.choices,
        db_index=True,
        help_text="Why this message was sent. Drives the retention window.",
    )
    recipient = models.EmailField(
        max_length=254,
        db_index=True,
        help_text=(
            "The address the message went to. The primary way these rows are "
            "found, because a signup or pre-tenant reset has no user row to "
            "search by."
        ),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="email_delivery_logs",
        help_text=(
            "The account this concerned, when there is one. Null for signup and "
            "pre-tenant password reset, which act on a TempUser."
        ),
    )
    temp_user_id = models.UUIDField(
        null=True,
        blank=True,
        db_index=True,
        help_text=(
            "The in-progress signup this message belongs to, for the flows that run "
            "before a CustomUser exists. This is what ties a signup journey "
            "together when its email address changes part-way through."
        ),
    )
    tenant_id = models.UUIDField(
        null=True,
        blank=True,
        db_index=True,
        help_text=(
            "Tenant this message related to, when one existed. Null before signup "
            "completes."
        ),
    )

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
        help_text="Delivery outcome. SENT means the relay accepted it, not that it arrived.",
    )
    sent_at = models.DateTimeField(
        null=True, blank=True, help_text="When the send completed."
    )
    error = models.TextField(
        blank=True, default="", help_text="Failure detail, truncated."
    )

    ip_address = models.GenericIPAddressField(
        null=True,
        blank=True,
        help_text=(
            "Where the request that triggered this came from. The reason logging "
            "password resets is worth anything: 'who asked for this' is the "
            "question actually asked after an account is compromised."
        ),
    )
    user_agent = models.TextField(
        blank=True, default="", help_text="Requesting client, truncated."
    )

    class Meta:
        db_table = "user_email_delivery_logs"
        verbose_name = "Email Delivery Log"
        verbose_name_plural = "Email Delivery Logs"
        ordering = ["-created_at"]
        indexes = [
            # "Every message we sent to this address" — the support question.
            models.Index(
                fields=["recipient", "-created_at"], name="edl_recipient_recent_idx"
            ),
            # Retention purges per purpose, and each purpose has its own window.
            models.Index(
                fields=["purpose", "created_at"], name="edl_purpose_created_idx"
            ),
            # "Everything sent about this account" — the security-audit question.
            models.Index(fields=["user", "-created_at"], name="edl_user_recent_idx"),
            # The whole of one signup attempt, including messages sent before and
            # after its email address changed.
            models.Index(
                fields=["temp_user_id", "-created_at"], name="edl_tempuser_recent_idx"
            ),
        ]

    # There is deliberately NO dedup_key and no unique constraint. Resending an OTP
    # is a feature with its own button, and a second row for it is the truth, not a
    # duplicate. This is the clearest way the model differs from NotificationLog.

    def mark_sent(self, when):
        self.status = self.Status.SENT
        self.sent_at = when
        self.error = ""
        self.save(update_fields=["status", "sent_at", "error", "updated_at"])

    def mark_failed(self, error):
        self.status = self.Status.FAILED
        self.error = (error or "")[:2000]
        self.save(update_fields=["status", "error", "updated_at"])

    def __str__(self):
        return f"{self.purpose} → {self.recipient} ({self.status})"
