from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator, MinLengthValidator
from django.db import models, transaction
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _

from core.models.catalog_variant import CatalogVariant
from tenant.common.models.tenant_base_model import TenantBaseModel


class CatalogItem(TenantBaseModel):
    """
    One thing the tenant sells — the nameable, describable half of it. Tier 2
    (fully audited + soft delete).

    **Not the priced half.** Price, stock, SKU and unit live on
    :class:`core.CatalogVariant`, because most real catalog lines have more than one
    price for one name: Half and Full, Small/Medium/Large, 1 kg / 5 kg / 50 kg bag,
    Adult and Child. Putting a price here and letting variants "override" it would
    give every consumer two places to read from, and the one that reads the wrong
    one is the voice agent quoting a caller.

    Two independent axes describe an offering, and keeping them apart is what lets
    one table serve every business type:

    * ``item_kind`` — **what it is.** A good, a service, a bundle, an add-on.
    * ``pricing_type`` / ``unit_of_measure`` / ``recurring_interval`` on the variant
      — **how it is charged.**

    A rented excavator is a physical good charged hourly. A monthly rent is a
    property charged recurringly. A gym membership is a service charged yearly.
    Collapsed into a single ``type`` enum, each of those needs a new value and a
    migration; split, they are combinations of values that already exist.
    """

    class ItemKind(models.TextChoices):
        PHYSICAL_GOOD = "PHYSICAL_GOOD", _("Physical good")
        DIGITAL_GOOD = "DIGITAL_GOOD", _("Digital good")
        SERVICE = "SERVICE", _("Service")
        BUNDLE = "BUNDLE", _("Bundle / package")
        ADD_ON = "ADD_ON", _("Add-on")
        OTHER = "OTHER", _("Other")

    class FulfilmentMode(models.TextChoices):
        AT_BUSINESS = "AT_BUSINESS", _("At the business")
        AT_CUSTOMER = "AT_CUSTOMER", _("At the customer's location")
        REMOTE = "REMOTE", _("Remote / online")
        DELIVERY = "DELIVERY", _("Delivery")
        PICKUP = "PICKUP", _("Pickup / takeaway")
        DINE_IN = "DINE_IN", _("Dine-in")

    item_kind = models.CharField(
        max_length=20,
        choices=ItemKind.choices,
        db_index=True,
        help_text=_(
            "What this is — never how it is charged. No default: forcing the choice "
            "is what stops a catalog quietly filing everything under one kind."
        ),
    )
    category = models.ForeignKey(
        "core.CatalogCategory",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="items",
        help_text=_(
            "Optional on purpose — a tenant must be able to add their first product "
            "before designing a taxonomy. SET_NULL so removing a section orphans "
            "items rather than deleting them."
        ),
    )

    name = models.CharField(
        max_length=200,
        validators=[MinLengthValidator(2), MaxLengthValidator(200)],
        help_text=_(
            "Deliberately not unique. Unlike an Agent or an EventTemplate — "
            "configuration objects a user names — a catalog is a data set, and "
            "'Basmati Rice 5 kg' from two brands is a correct pair of rows."
        ),
    )
    item_code = models.CharField(
        max_length=50,
        blank=True,
        default="",
        help_text=_(
            "The tenant's own reference, e.g. 'PRD-1024'. Tenant-supplied, never "
            "generated. Unique per tenant when present."
        ),
    )

    short_description = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=_(
            "The one sentence the agent says out loud. Separate from `description` "
            "because a bot reading 800 words at a caller is a defect, and truncating "
            "prose mid-clause sounds broken."
        ),
    )
    description = models.TextField(
        blank=True,
        default="",
        help_text=_("Long form — dashboard and tenant website."),
    )
    brand = models.CharField(
        max_length=150,
        blank=True,
        default="",
        help_text=_(
            "A real column rather than an `attributes` key because it is both "
            "filtered and spoken: 'do you have Tata salt?'"
        ),
    )

    tags = ArrayField(
        models.CharField(max_length=50),
        default=list,
        blank=True,
        help_text=_("Cross-cutting labels — 'bestseller', 'organic'. These categorise."),
    )
    aliases = ArrayField(
        models.CharField(max_length=100),
        default=list,
        blank=True,
        help_text=_(
            "What callers actually say: 'coke' for 'Coca-Cola 500 ml', 'teeth "
            "cleaning' for 'Scaling & Polishing'. Speech recognition returns the "
            "caller's words, not the catalog's. These recognise, which is a "
            "different job from what `tags` does."
        ),
    )
    fulfilment_modes = ArrayField(
        models.CharField(max_length=20, choices=FulfilmentMode.choices),
        default=list,
        blank=True,
        help_text=_(
            "How it can be had. An array because a restaurant is genuinely all of "
            "dine-in, delivery and pickup, and 'do you do home visits?' is a "
            "top-five inbound question for salons, clinics and repair trades."
        ),
    )
    attributes = models.JSONField(
        default=dict,
        blank=True,
        help_text=_(
            "The industry-specific long tail — {'bhk': 3, 'carpet_area_sqft': 1150}, "
            "{'veg': true, 'allergens': ['nuts']}. Columns for every industry's "
            "fields is unbounded; EAV is three tables and a join per attribute. "
            "Nothing validates the keys — that is the accepted cost, bounded by a "
            "byte cap in the serializer."
        ),
    )

    event_template = models.ForeignKey(
        "core.EventTemplate",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="catalog_items",
        help_text=_(
            "Set means this service is bookable, and duration, buffers, notice and "
            "horizon are read from the template — never duplicated here. Two "
            "durations that can disagree surface as a double-booked host."
        ),
    )
    add_on_items = models.ManyToManyField(
        "self",
        symmetrical=False,
        blank=True,
        related_name="add_on_for",
        help_text=_(
            "What may be added on top — 'extra cheese', 'extended warranty', "
            "'airport transfer'. Targets must be item_kind=ADD_ON. Not symmetrical: "
            "cheese goes on a burger, not the reverse."
        ),
    )

    lead_time_minutes = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_(
            "How long until it can be handed over or started — restaurant prep time, "
            "made-to-order lead time, 'ready in 2 days' (2880). Minutes throughout so "
            "there is one unit rather than two."
        ),
    )
    available_from = models.DateField(
        null=True,
        blank=True,
        help_text=_("Start of a seasonal listing window. Null means always."),
    )
    available_until = models.DateField(
        null=True,
        blank=True,
        help_text=_("End of the window. Null means indefinitely."),
    )

    pronunciation_hint = models.CharField(
        max_length=150,
        blank=True,
        default="",
        help_text=_(
            "TTS spelling for names the synthesiser mangles — 'Xylo-Guard 5G', "
            "'Levocetirizine'."
        ),
    )
    is_featured = models.BooleanField(
        default=False,
        help_text=_(
            "'What do you recommend?' needs an answer that is not 'the most recently "
            "created row'."
        ),
    )
    sort_order = models.PositiveIntegerField(
        default=0,
        help_text=_("Manual ordering within a category."),
    )

    class Meta:
        db_table = "core_catalog_items"
        verbose_name = _("Catalog Item")
        verbose_name_plural = _("Catalog Items")
        ordering = ["sort_order", "-created_at"]
        constraints = [
            # Partial twice over. Excluding deleted rows stops a removed item burning
            # its code forever; excluding "" is what makes the field optional at all,
            # since two empty strings collide under a plain unique constraint.
            models.UniqueConstraint(
                Lower("item_code"),
                "tenant",
                condition=models.Q(is_deleted=False) & ~models.Q(item_code=""),
                name="unique_active_catalog_item_code_tenant",
            ),
            models.CheckConstraint(
                condition=models.Q(available_until__isnull=True)
                | models.Q(available_from__isnull=True)
                | models.Q(available_until__gte=models.F("available_from")),
                name="catalog_item_availability_window_ordered",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "category", "sort_order"]),
            models.Index(fields=["tenant", "item_kind"]),
            models.Index(fields=["tenant", "is_active"]),
            GinIndex(fields=["tags"], name="catalog_item_tags_gin"),
            GinIndex(fields=["aliases"], name="catalog_item_aliases_gin"),
            GinIndex(fields=["attributes"], name="catalog_item_attributes_gin"),
        ]

    def __str__(self):
        return self.name

    # --- variants -------------------------------------------------------------

    @property
    def default_variant(self):
        """The variant quoted when the caller does not specify one.

        Scans ``self.variants.all()`` in Python rather than filtering in SQL, and the
        difference is not stylistic: ``prefetch_related`` only ever populates the cache
        for ``.all()``, so a ``.filter(is_default=True)`` here issued a fresh query for
        **every row** of the item list — the exact N+1 the prefetch was added to
        prevent, on the most-hit endpoint in the module.

        Scanning costs nothing: ``CATALOG_MAX_VARIANTS_PER_ITEM`` bounds the list, and
        without a prefetch this is the same single query it always was.
        """
        for variant in self.variants.all():
            if variant.is_default:
                return variant
        return None

    def ensure_default_variant(self):
        """Guarantee this item has exactly one default variant, and return it.

        The "every item has at least one variant" invariant cannot be a database
        constraint — the first variant would be unsavable, since the item must exist
        before anything can point at it. So it is enforced in the write path, and
        this is that path: serializers call it after saving nested variants, the
        admin calls it after the inline formset, and a shell session can call it
        directly.

        Deliberately **not** a ``post_save`` signal. A signal would fire on the
        item's own INSERT, before the API had a chance to write the variants the
        request actually contained — leaving a junk "Standard" row alongside them and
        fighting over ``is_default``.

        An auto-created variant is ``ON_REQUEST``, not a zero price. "We have not
        priced this yet" is the truth, and ``FREE`` or ``FIXED 0.00`` would have the
        agent quote it as free.
        """
        variants = self.variants.all()
        existing_default = variants.filter(is_default=True).first()
        if existing_default is not None:
            return existing_default

        promotable = variants.order_by("sort_order", "created_at").first()
        if promotable is not None:
            promotable.is_default = True
            promotable.save(update_fields=["is_default"])
            return promotable

        return CatalogVariant.objects.create(
            tenant_id=self.tenant_id,
            item=self,
            variant_label="Standard",
            is_default=True,
            pricing_type=CatalogVariant.PricingType.ON_REQUEST,
        )

    # --- validation -----------------------------------------------------------

    def clean(self):
        super().clean()

        if self.category is not None and self.category.tenant_id != self.tenant_id:
            raise ValidationError(
                {"category": _("That category belongs to a different tenant.")}
            )

        if self.event_template is not None:
            if self.event_template.tenant_id != self.tenant_id:
                raise ValidationError(
                    {
                        "event_template": _(
                            "That event template belongs to a different tenant."
                        )
                    }
                )
            if self.item_kind != self.ItemKind.SERVICE:
                raise ValidationError(
                    {
                        "event_template": _(
                            "Only a service can be bookable. A bookable bag of "
                            "fertilizer is a data-entry mistake, not a business model."
                        )
                    }
                )

    # --- lifecycle ------------------------------------------------------------

    def soft_delete(self, user=None):
        """Remove the item and take its variants and media with it.

        A variant of a delisted item left ``is_active=True`` is exactly the orphan
        that gets quoted at a caller months later. Media follows for the same reason
        the KnowledgeBase cascade exists: nothing should outlive the row it belongs
        to and keep serving presigned URLs.
        """
        with transaction.atomic():
            CatalogItem.objects.select_for_update().get(pk=self.pk)

            super().soft_delete(user=user)

            for variant in self.variants.all():
                variant.soft_delete(user=user)
            for media in self.media.all():
                media.soft_delete(user=user)

    def restore(self):
        """Un-delete, refusing to create a duplicate ``item_code``.

        Only relevant when a code is set: the constraint excludes blanks, so an
        item without one can always come back.
        """
        if self.item_code:
            clash = (
                CatalogItem.objects.filter(
                    tenant_id=self.tenant_id, item_code__iexact=self.item_code
                )
                .exclude(pk=self.pk)
                .exists()
            )
            if clash:
                raise ValidationError(
                    {
                        "item_code": _(
                            "Another active item already uses this code. Change or "
                            "remove that item before restoring this one."
                        )
                    }
                )
        super().restore()
