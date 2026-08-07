from django.db import models
from django.utils.translation import gettext_lazy as _

from tenant.common.models.tenant_base_model import TenantBaseModel
from utils.phone import PhoneNormalizationError, normalize_phone


class DirectoryContact(TenantBaseModel):
    """
    A person who works at this tenant — the company directory. Tier 2 (fully
    audited + soft delete).

    **Not** :class:`core.Contact`. That model is people the tenant *calls*: leads
    and customers, with consent tracking, ``do_not_call``, and campaign
    membership. This one is the tenant's own staff, and nothing here is ever a
    campaign target. Two models a single adjective apart would leave "which
    contacts table?" a permanent question, so the names carry the distinction.

    Identity here is deliberately loose. ``Contact`` keys on a normalized phone
    number because deciding "is this the same person" drives deduplication and
    inbound-caller lookup. A directory has no such job: rows are created by hand,
    read by humans, and the only uniqueness worth enforcing is that one email
    address does not appear twice.
    """

    class Category(models.TextChoices):
        EXECUTIVE = "EXECUTIVE", _("Executive")
        OPERATIONS = "OPERATIONS", _("Operations")

    name = models.CharField(
        max_length=150,
        help_text=_(
            "Full name, as it should be displayed. One field rather than "
            "first/last: directory entries render as a single line, and titles "
            "like 'Dr. R. Sharma' do not split cleanly."
        ),
    )
    designation = models.CharField(
        max_length=150,
        help_text=_("Job title, e.g. 'VP of Engineering'. Shown and searched."),
    )
    category = models.CharField(
        max_length=20,
        choices=Category.choices,
        default=Category.OPERATIONS,
        db_index=True,
        help_text=_(
            "Which group this person is listed under. Set explicitly by the "
            "tenant — never inferred from `designation`. A stored, indexed column "
            "so listing a group is an index lookup, and so renaming someone's "
            "title cannot silently move them between groups."
        ),
    )
    department = models.CharField(
        max_length=100,
        blank=True,
        help_text=_("Optional grouping, e.g. 'Support'. Independent of category."),
    )

    email = models.EmailField(
        max_length=254,
        blank=True,
        help_text=_(
            "Optional. Reception and shop-floor entries legitimately have none, "
            "and a required-and-unique address would make those un-addable."
        ),
    )

    phone_number = models.CharField(
        max_length=32,
        blank=True,
        help_text=_("The number as supplied, preserved for display."),
    )
    phone_e164 = models.CharField(
        max_length=20,
        blank=True,
        db_index=True,
        help_text=_(
            "Normalized E.164 form, derived on save. Blank when the number could "
            "not be parsed — see `_derive_phone`. Never set this directly."
        ),
    )

    class Meta:
        db_table = "core_directory_contacts"
        verbose_name = _("Directory Contact")
        verbose_name_plural = _("Directory Contacts")
        ordering = ["name"]
        constraints = [
            # Partial twice over. Excluding soft-deleted rows stops a removed entry
            # burning its address forever; excluding blanks is what makes `email`
            # optional at all, since two empty strings collide under a plain
            # unique_together.
            models.UniqueConstraint(
                fields=["tenant", "email"],
                condition=models.Q(is_deleted=False) & ~models.Q(email=""),
                name="unique_active_directory_contact_email",
            ),
        ]
        indexes = [
            # The list is filtered by category far more than anything else.
            models.Index(fields=["tenant", "category"]),
            models.Index(fields=["tenant", "email"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.designation})" if self.designation else self.name

    def _derive_phone(self, region=None):
        """Fill `phone_e164` from `phone_number`, best effort.

        Deliberately unlike :meth:`core.Contact.normalize`, which raises. There the
        normalized number *is* the identity, so an unparseable one has to be
        rejected. Here it is a convenience for dialling, and a number a human can
        read is still worth storing — rejecting the whole entry because someone
        wrote "x2214" or "+91 22 1234 (ask for Priya)" would lose real information
        to gain none.
        """
        if not self.phone_number:
            self.phone_e164 = ""
            return

        try:
            self.phone_e164 = normalize_phone(self.phone_number, region=region)[0]
        except PhoneNormalizationError:
            self.phone_e164 = ""

    def save(self, *args, **kwargs):
        region = kwargs.pop("region", None)
        update_fields = kwargs.get("update_fields")

        # Derive from the raw value whenever it is in play; a caller-supplied
        # phone_e164 is never trusted. Targeted housekeeping saves (soft_delete,
        # activate, restore) skip it, so a row can always be removed or
        # deactivated regardless of what its number looks like.
        if update_fields is None or "phone_number" in update_fields:
            self._derive_phone(region=region)
            if update_fields is not None:
                kwargs["update_fields"] = list(
                    dict.fromkeys([*update_fields, "phone_e164"])
                )

        super().save(*args, **kwargs)
