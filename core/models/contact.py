from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from tenant.common.models.tenant_base_model import TenantBaseModel
from utils.phone import PhoneNormalizationError, normalize_phone


class Contact(TenantBaseModel):
    """
    A person this tenant can call or book. Tier 2 (fully audited + soft delete).

    Identity is `phone_e164`, not the raw `phone_number` the user typed. Everything
    that needs to decide "is this the same person" — uniqueness, import conflict
    detection, inbound-caller lookup — must key off the normalized column.
    """

    class Source(models.TextChoices):
        MANUAL = "MANUAL", "Created manually"
        IMPORT = "IMPORT", "Bulk import"
        API = "API", "API"
        CALL = "CALL", "Captured from a call"

    class ConsentStatus(models.TextChoices):
        UNKNOWN = "UNKNOWN", "Unknown"
        GRANTED = "GRANTED", "Granted"
        DENIED = "DENIED", "Denied"
        WITHDRAWN = "WITHDRAWN", "Withdrawn"

    first_name = models.CharField(max_length=120, blank=True)
    last_name = models.CharField(max_length=120, blank=True)

    phone_number = models.CharField(
        max_length=32,
        help_text=_("The number as supplied, preserved for display."),
    )
    phone_e164 = models.CharField(
        max_length=20,
        db_index=True,
        help_text=_("Normalized E.164 form. Derived on save; never set this directly."),
    )
    phone_region = models.CharField(
        max_length=2,
        blank=True,
        help_text=_("ISO-3166 alpha-2 region resolved during normalization."),
    )

    email_address = models.EmailField(max_length=254, blank=True)
    company = models.CharField(max_length=200, blank=True)
    tags = ArrayField(
        models.CharField(max_length=50),
        default=list,
        blank=True,
        help_text=_("Free-form labels. Filterable via ?tags__contains=."),
    )
    notes = models.TextField(blank=True)

    source = models.CharField(
        max_length=10,
        choices=Source.choices,
        default=Source.MANUAL,
        help_text=_("How this contact entered the system."),
    )

    # Consent and suppression. Written through the API or admin with attribution —
    # deliberately never writable by bulk import, so re-uploading a stale list cannot
    # resurrect someone who opted out.
    consent_status = models.CharField(
        max_length=10,
        choices=ConsentStatus.choices,
        default=ConsentStatus.UNKNOWN,
    )
    consent_source = models.CharField(max_length=200, blank=True)
    consent_at = models.DateTimeField(null=True, blank=True)
    do_not_call = models.BooleanField(
        default=False,
        db_index=True,
        help_text=_("Hard suppression. The dialler must check this before calling."),
    )
    opted_out_at = models.DateTimeField(null=True, blank=True)

    last_contacted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Maintained by the voice runtime, not by this module."),
    )

    class Meta:
        db_table = "core_contact"
        verbose_name = "Contact"
        verbose_name_plural = "Contacts"
        ordering = ["-created_at"]
        constraints = [
            # Partial, so soft-deleting a contact does not permanently burn its number.
            models.UniqueConstraint(
                fields=["tenant", "phone_e164"],
                condition=models.Q(is_deleted=False),
                name="unique_active_contact_phone_tenant",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "phone_e164"]),
            models.Index(fields=["tenant", "email_address"]),
            GinIndex(fields=["tags"]),
        ]

    def __str__(self):
        name = f"{self.first_name} {self.last_name}".strip()
        return name or self.phone_e164 or str(self.id)

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def normalize(self, region=None):
        """
        Derive `phone_e164` / `phone_region` from `phone_number`.

        Raises ValidationError (not PhoneNormalizationError) so DRF and the admin both
        surface it as a field error rather than a 500.
        """
        try:
            e164, region_code = normalize_phone(self.phone_number, region=region)
        except PhoneNormalizationError as exc:
            raise ValidationError({"phone_number": str(exc)})

        self.phone_e164 = e164
        self.phone_region = region_code

    def save(self, *args, **kwargs):
        region = kwargs.pop("region", None)
        update_fields = kwargs.get("update_fields")

        # Derive from the raw value whenever it is in play — a caller-supplied
        # phone_e164 is never trusted. Targeted housekeeping saves (soft_delete,
        # activate, restore) skip it, so a row with a stale or now-invalid number
        # can still be deleted or deactivated instead of being stuck forever.
        if update_fields is None or "phone_number" in update_fields:
            self.normalize(region=region)
            if update_fields is not None:
                kwargs["update_fields"] = list(
                    dict.fromkeys([*update_fields, "phone_e164", "phone_region"])
                )

        super().save(*args, **kwargs)

    def restore(self):
        """
        Un-delete, refusing to create a duplicate.

        The unique constraint only covers live rows, so a number freed by soft-deleting
        this contact may have been taken since. Without this check the restore raises a
        raw IntegrityError out of the admin.
        """
        clash = (
            Contact.objects.filter(
                tenant_id=self.tenant_id, phone_e164=self.phone_e164
            )
            .exclude(pk=self.pk)
            .exists()
        )
        if clash:
            raise ValidationError(
                {
                    "phone_number": (
                        "Another active contact already uses this phone number. "
                        "Change or remove that contact before restoring this one."
                    )
                }
            )
        super().restore()
