from django.core.exceptions import ValidationError
from django.db import models

from tenant.common.models.tenant_base_model import TenantBaseModel


class TenantSettings(TenantBaseModel):
    """
    Per-tenant configuration that is not a credential and not part of the tenant's
    identity — the knobs a tenant sets for how the product behaves for them.

    One row per tenant, created on demand the first time something is set.

    Today it holds exactly one setting: which SMTP configuration carries booking
    email. More will land here; the model exists so the next one is a field rather
    than a table.

    **Booking email does not fall back to the platform sender.** If
    ``booking_email_configuration`` is unset — or points at something that has been
    disconnected or has never passed a test send — booking mail is **not sent at
    all**. That is deliberate: a confirmation arriving from an address the customer
    has never heard of is worse for the tenant's brand than one that does not
    arrive, and silently borrowing the platform's sending reputation for a tenant's
    customer mail is exactly what this whole feature exists to stop.
    """

    booking_email_configuration = models.ForeignKey(
        "tenant.TenantEmailConfiguration",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="booking_settings",
        help_text=(
            "The SMTP configuration used for booking email — confirmations, "
            "cancellations, reminders, reschedule requests. Unset means booking "
            "email is not sent."
        ),
    )

    credits_enforced = models.BooleanField(
        default=False,
        help_text=(
            "Whether a shortfall of credits actually blocks work for this tenant. "
            "Metering runs regardless — every call is rated and written to the credit "
            "ledger from day one — but while this is False a tenant who cannot afford "
            "an action is only logged, never refused. It exists so the credit rates can "
            "be checked against a full cycle of real traffic before a wrong number "
            "stops a paying customer's campaign, and it doubles as a per-tenant escape "
            "hatch when a billing bug needs unblocking without disabling metering for "
            "everyone."
        ),
    )

    class Meta(TenantBaseModel.Meta):
        db_table = "tenant_settings"
        verbose_name = "Tenant Settings"
        verbose_name_plural = "Tenant Settings"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant"],
                condition=models.Q(is_deleted=False),
                name="unique_active_tenant_settings",
            ),
        ]

    # --- access ---------------------------------------------------------------

    @classmethod
    def for_tenant(cls, tenant_id):
        """This tenant's settings row, creating it if it does not exist yet.

        Settings are read on paths that must not fail — including a Celery task
        deciding whether a booking confirmation can be sent — so "no row yet" has
        to behave like "all defaults", never like an error.
        """
        settings_row, _ = cls.objects.get_or_create(tenant_id=tenant_id)
        return settings_row

    @classmethod
    def booking_sender(cls, tenant_id):
        """The configuration that may carry this tenant's booking mail, or ``None``.

        All four conditions, checked here rather than trusted from the pointer:
        assigned, not deleted, connected, and verified. The pointer is set once and
        read for months — in between, the configuration can be disconnected, or a
        credential edit can reset it to ``UNVERIFIED``. Re-checking at read time is
        what stops a stale pointer from being treated as a working sender.

        ``None`` means **do not send**. It does not mean "use the platform sender".
        """
        from tenant.models.TenantEmailConfiguration import TenantEmailConfiguration

        row = cls.objects.filter(tenant_id=tenant_id).first()
        if row is None or row.booking_email_configuration_id is None:
            return None
        return (
            TenantEmailConfiguration.objects.filter(
                pk=row.booking_email_configuration_id,
                tenant_id=tenant_id,
                is_active=True,
                status=TenantEmailConfiguration.Status.VERIFIED,
            )
            .select_related("tenant")
            .first()
        )

    # --- validation -----------------------------------------------------------

    def clean(self):
        """Refuse a booking sender that belongs to someone else.

        The pointer arrives as an id. Without this, a tenant could name another
        tenant's configuration and send their own customer mail through a stranger's
        credentials — a cross-tenant write chosen entirely by a request field.
        """
        super().clean()
        config = self.booking_email_configuration
        if config is not None and config.tenant_id != self.tenant_id:
            raise ValidationError(
                {
                    "booking_email_configuration": (
                        "That email configuration belongs to a different tenant."
                    )
                }
            )

    def __str__(self):
        return f"Settings for {self.tenant_id}"
