from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from tenant.common.models.tenant_base_model import TenantBaseModel

# Presentation only, and only for the currencies this product actually sees. An
# unknown code falls back to the code itself, which is unambiguous if less pretty —
# better than guessing a symbol two currencies share.
_CURRENCY_SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}


def default_catalog_currency():
    """Read at write time, not import time, so a settings override actually takes.

    A callable also keeps the value out of the migration: Django serialises the
    function reference, so changing CATALOG_DEFAULT_CURRENCY does not need one.
    """
    return getattr(settings, "CATALOG_DEFAULT_CURRENCY", "INR")


class CatalogVariant(TenantBaseModel):
    """
    The priceable, sellable unit of a :class:`core.CatalogItem`. Tier 2.

    Every item has at least one — single-price items get an auto-created
    ``Standard`` variant (see ``CatalogItem.ensure_default_variant``) — so that no
    consumer ever has to ask "does this item have variants, or is the price on the
    parent?". The join costs one query; the branch would cost a wrong quote.

    Tier 2 rather than Tier 1 for one reason: **price lives here**, and "who set this
    to ₹1?" is the question this module most has to be able to answer.

    Six pricing shapes, because businesses charge in six shapes:

    ==============  ==========================================================
    ``FIXED``       ₹380. The ordinary case.
    ``FROM``        "from ₹50,000" — project work scoped per enquiry.
    ``RANGE``       ₹2,000–₹5,000, both ends known.
    ``RECURRING``   ₹12,000 per year — memberships, rent, AMCs, retainers.
    ``RATE``        8.4% per annum — loans, brokerage, commission.
    ``ON_REQUEST``  Quote only. ``price`` is null and that is correct.
    ``FREE``        Lead-gen consultations, samples.
    ==============  ==========================================================

    ``base_price`` cuts across all of them: set it and the total reads
    ``base_price + (price × quantity)``, which is how a taxi (₹50 base + ₹18/km) and
    a call-out trade (₹300 visit + ₹150/unit) actually quote.
    """

    class PricingType(models.TextChoices):
        FIXED = "FIXED", _("Fixed price")
        FROM = "FROM", _("Starting from")
        RANGE = "RANGE", _("Price range")
        RECURRING = "RECURRING", _("Recurring")
        RATE = "RATE", _("Percentage rate")
        ON_REQUEST = "ON_REQUEST", _("Price on request")
        FREE = "FREE", _("Free")

    class UnitOfMeasure(models.TextChoices):
        # Count
        PIECE = "PIECE", _("piece")
        DOZEN = "DOZEN", _("dozen")
        PACK = "PACK", _("pack")
        BOX = "BOX", _("box")
        # Weight & volume
        GRAM = "GRAM", _("gram")
        KG = "KG", _("kilogram")
        QUINTAL = "QUINTAL", _("quintal")
        TONNE = "TONNE", _("tonne")
        ML = "ML", _("millilitre")
        LITRE = "LITRE", _("litre")
        # Distance & area
        METRE = "METRE", _("metre")
        KM = "KM", _("kilometre")
        SQ_FT = "SQ_FT", _("square foot")
        SQ_METRE = "SQ_METRE", _("square metre")
        ACRE = "ACRE", _("acre")
        # Time
        MINUTE = "MINUTE", _("minute")
        HOUR = "HOUR", _("hour")
        DAY = "DAY", _("day")
        NIGHT = "NIGHT", _("night")
        WEEK = "WEEK", _("week")
        MONTH = "MONTH", _("month")
        YEAR = "YEAR", _("year")
        # Service
        SESSION = "SESSION", _("session")
        VISIT = "VISIT", _("visit")
        TRIP = "TRIP", _("trip")
        PERSON = "PERSON", _("person")
        # Rate
        PERCENT = "PERCENT", _("percent")
        # Escape hatch
        OTHER = "OTHER", _("other")

    class RecurringInterval(models.TextChoices):
        DAY = "DAY", _("day")
        WEEK = "WEEK", _("week")
        MONTH = "MONTH", _("month")
        QUARTER = "QUARTER", _("quarter")
        YEAR = "YEAR", _("year")

    class AvailabilityStatus(models.TextChoices):
        IN_STOCK = "IN_STOCK", _("In stock")
        LOW_STOCK = "LOW_STOCK", _("Low stock")
        OUT_OF_STOCK = "OUT_OF_STOCK", _("Out of stock")
        PREORDER = "PREORDER", _("Available to pre-order")
        MADE_TO_ORDER = "MADE_TO_ORDER", _("Made to order")
        SEASONAL = "SEASONAL", _("Seasonal")
        DISCONTINUED = "DISCONTINUED", _("Discontinued")

    #: Statuses that mean "not sellable right now", whatever the stock says.
    UNSELLABLE_STATUSES = frozenset(
        {AvailabilityStatus.OUT_OF_STOCK, AvailabilityStatus.DISCONTINUED}
    )

    # --- identity & structure -------------------------------------------------

    item = models.ForeignKey(
        "core.CatalogItem",
        on_delete=models.CASCADE,
        related_name="variants",
        help_text=_("Hard cascade: a variant has no meaning without its item."),
    )
    variant_label = models.CharField(
        max_length=120,
        default="Standard",
        help_text=_(
            "What makes this one different, in the words the agent will say: "
            "'Large', '50 kg bag', 'Adult', 'Home visit'."
        ),
    )
    options = models.JSONField(
        default=dict,
        blank=True,
        help_text=_(
            "The structured form of the same thing — {'size': 'L', 'colour': 'Red'} "
            "— for filtering and grid UIs."
        ),
    )
    sku = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=_(
            "Optional: service businesses have none. Unique per tenant, "
            "case-insensitively, when present."
        ),
    )
    barcode = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=_(
            "EAN/UPC/GTIN. Deliberately not unique — two rows can legitimately "
            "resell the same barcoded product."
        ),
    )
    is_default = models.BooleanField(
        default=False,
        help_text=_(
            "The one quoted when the caller does not specify. At most one per item, "
            "enforced in the database."
        ),
    )
    sort_order = models.PositiveIntegerField(
        default=0,
        help_text=_("Small before Large, not alphabetical."),
    )

    # --- pricing --------------------------------------------------------------

    pricing_type = models.CharField(
        max_length=20,
        choices=PricingType.choices,
        default=PricingType.FIXED,
        help_text=_("How this is charged — the axis independent of `item_kind`."),
    )
    price = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_(
            "The amount, or — when `base_price` is set — the per-unit rate, or, for "
            "RATE, the percentage. 14 digits because a property in INR and a car in "
            "IDR both have to fit; Plan.base_price's (10, 2) caps at ~₹99 lakh."
        ),
    )
    base_price = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_(
            "The fixed component of a composite price — call-out charge, base fare, "
            "minimum. Total reads base_price + (price × quantity). Null for every "
            "ordinary product, which is most of them."
        ),
    )
    max_price = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("Upper bound, for RANGE pricing."),
    )
    compare_at_price = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_(
            "MRP / strike-through, so a discount is expressible without a "
            "promotions model."
        ),
    )
    currency = models.CharField(
        max_length=3,
        default=default_catalog_currency,
        validators=[
            # Case-INsensitive on purpose. `save()` upper-cases, so the stored value is
            # always canonical — but a strict `^[A-Z]{3}$` here runs during DRF's field
            # validation, which happens *before* the serializer's `validate_currency`
            # gets a chance to normalise. The result was an API that rejected "usd"
            # instead of accepting it as USD, while the shell happily stored it.
            RegexValidator(
                regex=r"^[A-Za-z]{3}$",
                message=_("Currency must be a three-letter ISO-4217 code."),
            )
        ],
        help_text=_(
            "ISO-4217, stored upper-case. Not a TextChoices: baking a currency list "
            "into a migration has the same problem the BusinessHours.timezone comment "
            "records for timezones."
        ),
    )

    unit_of_measure = models.CharField(
        max_length=20,
        choices=UnitOfMeasure.choices,
        default=UnitOfMeasure.PIECE,
        help_text=_("The unit the price is quoted against."),
    )
    unit_label = models.CharField(
        max_length=50,
        blank=True,
        default="",
        help_text=_(
            "The words for the invoice line and the spoken sentence: 'per 50 kg "
            "bag', 'per plate', '% per annum of loan amount'. `unit_of_measure` is "
            "for logic; this is for humans and TTS."
        ),
    )

    recurring_interval = models.CharField(
        max_length=10,
        choices=RecurringInterval.choices,
        null=True,
        blank=True,
        help_text=_("Required for RECURRING, and must be null otherwise."),
    )
    recurring_interval_count = models.PositiveSmallIntegerField(
        default=1,
        help_text=_("'every 3 months' is MONTH x 3."),
    )

    deposit_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_(
            "Refundable deposit — equipment rental, rented property, event hire. "
            "Without it, rentals cannot be quoted honestly."
        ),
    )
    duration_minutes = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text=_(
            "Display duration for a service NOT wired to an EventTemplate. Must be "
            "null when the item has one — the template is the single source of truth "
            "for duration."
        ),
    )

    tax_rate_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("0-100. Matches Plan.tax_percentage's precision."),
    )
    is_tax_inclusive = models.BooleanField(
        default=True,
        help_text=_(
            "Defaults True for the Indian MRP convention: the number the tenant "
            "types is the number the caller pays. Wrong here, every quoted price is "
            "wrong by the tax rate."
        ),
    )
    tax_code = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text=_(
            "HSN (goods) / SAC (services). Free text — code lists change and this is "
            "not a tax engine."
        ),
    )

    # --- availability ---------------------------------------------------------

    availability_status = models.CharField(
        max_length=20,
        choices=AvailabilityStatus.choices,
        default=AvailabilityStatus.IN_STOCK,
        db_index=True,
        help_text=_(
            "Stated by the tenant, never derived from stock_quantity. The two "
            "disagree constantly in practice — '3 left but all reserved', 'out of "
            "stock but taking pre-orders' — and a derived column would silently "
            "overwrite what the tenant said."
        ),
    )
    track_inventory = models.BooleanField(
        default=False,
        help_text=_(
            "Opt in, so stock_quantity=0 never reads as 'out of stock' for a haircut."
        ),
    )
    stock_quantity = models.IntegerField(
        null=True,
        blank=True,
        help_text=_(
            "A number the tenant states, not a balance this module computes. "
            "Nothing here decrements it."
        ),
    )
    low_stock_threshold = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_("Where LOW_STOCK starts, for the tenant's own dashboard."),
    )
    min_order_quantity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        null=True,
        blank=True,
        help_text=_(
            "Wholesale minimums. Decimal because 'minimum 0.5 kg' is real. Stated, "
            "not enforced — there are no orders here to enforce it against."
        ),
    )
    quantity_increment = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        null=True,
        blank=True,
        help_text=_("'sold in multiples of 10'."),
    )

    class Meta:
        db_table = "core_catalog_variants"
        verbose_name = _("Catalog Variant")
        verbose_name_plural = _("Catalog Variants")
        ordering = ["sort_order", "created_at"]
        constraints = [
            models.UniqueConstraint(
                Lower("sku"),
                "tenant",
                condition=models.Q(is_deleted=False) & ~models.Q(sku=""),
                name="unique_active_catalog_variant_sku_tenant",
            ),
            # At most one default per item. "At least one" cannot be a constraint —
            # the first variant would be unsavable — and lives in the write path.
            models.UniqueConstraint(
                fields=["item"],
                condition=models.Q(is_deleted=False, is_default=True),
                name="unique_default_catalog_variant_per_item",
            ),
            # Money bounds live in the database, not only the serializer: the next
            # writer of this table will be a bulk importer or a data fix, not the API.
            models.CheckConstraint(
                condition=models.Q(price__isnull=True) | models.Q(price__gte=0),
                name="catalog_variant_price_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(base_price__isnull=True)
                | models.Q(base_price__gte=0),
                name="catalog_variant_base_price_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(deposit_amount__isnull=True)
                | models.Q(deposit_amount__gte=0),
                name="catalog_variant_deposit_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(max_price__isnull=True)
                | models.Q(price__isnull=True)
                | models.Q(max_price__gte=models.F("price")),
                name="catalog_variant_price_range_ordered",
            ),
            # A percentage is bounded even though a price is not. 8.4% p.a. is a
            # loan; 840% is a typo that would be read out to a caller as fact.
            models.CheckConstraint(
                condition=~models.Q(pricing_type="RATE")
                | (models.Q(price__gte=0) & models.Q(price__lte=100)),
                name="catalog_variant_rate_bounded",
            ),
            # pricing_type coherence — the shape of the row must match what it claims
            # to be. A RECURRING price with no interval is a row the agent cannot
            # read out loud.
            models.CheckConstraint(
                condition=(
                    ~models.Q(pricing_type__in=["FIXED", "FROM", "RECURRING", "RATE"])
                    | models.Q(price__isnull=False)
                ),
                name="catalog_variant_price_required_when_priced",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(pricing_type="RECURRING", recurring_interval__isnull=False)
                    | (
                        ~models.Q(pricing_type="RECURRING")
                        & models.Q(recurring_interval__isnull=True)
                    )
                ),
                name="catalog_variant_recurring_interval_coherent",
            ),
            models.CheckConstraint(
                condition=~models.Q(pricing_type="RANGE")
                | models.Q(max_price__isnull=False),
                name="catalog_variant_range_requires_max",
            ),
            models.CheckConstraint(
                condition=models.Q(stock_quantity__isnull=True)
                | models.Q(stock_quantity__gte=0),
                name="catalog_variant_stock_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(tax_rate_percentage__isnull=True)
                | (
                    models.Q(tax_rate_percentage__gte=0)
                    & models.Q(tax_rate_percentage__lte=100)
                ),
                name="catalog_variant_tax_rate_bounded",
            ),
        ]
        indexes = [
            models.Index(fields=["item", "sort_order"]),
            models.Index(fields=["tenant", "sku"]),
            models.Index(fields=["tenant", "availability_status"]),
        ]

    def __str__(self):
        return f"{self.item_id} / {self.variant_label}"

    # --- persistence ----------------------------------------------------------

    def save(self, *args, **kwargs):
        """Canonicalise the currency code on every write.

        `inr` and `INR` are the same currency, and letting both into the column makes
        `?currency=INR` silently miss rows and any future per-currency grouping wrong.
        Done here rather than only in the serializer so the shell, the admin, a
        management command and a data migration all land on the same value.
        """
        if self.currency:
            self.currency = self.currency.strip().upper()
        super().save(*args, **kwargs)

    # --- validation -----------------------------------------------------------

    def clean(self):
        super().clean()

        # `item` is non-nullable, so `self.item` raises RelatedObjectDoesNotExist
        # rather than returning None when it is unset. full_clean() calls clean()
        # even after clean_fields() has already recorded the missing FK, so without
        # this guard a blank form 500s instead of showing "this field is required".
        if self.item_id is not None:
            if self.item.tenant_id != self.tenant_id:
                raise ValidationError(
                    {"item": _("That item belongs to a different tenant.")}
                )

            if self.duration_minutes is not None and self.item.event_template_id:
                raise ValidationError(
                    {
                        "duration_minutes": _(
                            "This item is booked through an event template, which "
                            "already defines its duration. Two durations that can "
                            "disagree surface as a double-booked host."
                        )
                    }
                )

        if not self.track_inventory and self.stock_quantity is not None:
            raise ValidationError(
                {
                    "stock_quantity": _(
                        "Stock is not tracked for this variant. One representation "
                        "only — nothing may be both untracked and holding a count."
                    )
                }
            )

    # --- read model -----------------------------------------------------------

    @property
    def is_quotable(self):
        """Whether a number can be given without a human getting involved."""
        return self.pricing_type != self.PricingType.ON_REQUEST

    @property
    def is_sellable(self):
        """The single definition of "can we sell this right now?".

        Everything that decides it, in one place, so that voice, dashboard and any
        future integration cannot each answer it slightly differently. Reads
        ``self.item``, so callers iterating variants should ``select_related("item")``.
        """
        if not self.is_active or self.is_deleted:
            return False
        if self.availability_status in self.UNSELLABLE_STATUSES:
            return False
        if self.track_inventory and (self.stock_quantity or 0) <= 0:
            return False

        item = self.item
        if not item.is_active or item.is_deleted:
            return False

        today = timezone.localdate()
        if item.available_from and today < item.available_from:
            return False
        if item.available_until and today > item.available_until:
            return False

        return True

    def _money(self, amount):
        symbol = _CURRENCY_SYMBOLS.get(self.currency, f"{self.currency} ")
        quantized = Decimal(amount).quantize(Decimal("0.01"))
        # Trailing ".00" is noise in a spoken quote — "two thousand five hundred
        # rupees" not "two thousand five hundred point zero zero".
        if quantized == quantized.to_integral_value():
            return f"{symbol}{quantized.to_integral_value():,}"
        return f"{symbol}{quantized:,.2f}"

    @property
    def unit_phrase(self):
        """"per 50 kg bag" / "per hour" — the tenant's words, else the unit's."""
        if self.unit_label:
            return self.unit_label
        return f"per {self.get_unit_of_measure_display()}"

    @property
    def display_price(self):
        """One rendering of the price, for every consumer.

        The composite and rate shapes are exactly where two independent
        implementations would drift, so there is only one.
        """
        if self.pricing_type == self.PricingType.ON_REQUEST:
            return "Price on request"
        if self.pricing_type == self.PricingType.FREE:
            return "Free"
        if self.price is None:
            # Should be unreachable — the check constraints require a price for every
            # remaining type. Defensive, because a null here would otherwise raise
            # mid-sentence in a live call.
            return "Price on request"

        if self.pricing_type == self.PricingType.RATE:
            rate = self.price.normalize()
            basis = self.unit_label or "per annum"
            return f"{rate}% {basis}"

        if self.pricing_type == self.PricingType.RANGE:
            return f"{self._money(self.price)}–{self._money(self.max_price)}"

        if self.pricing_type == self.PricingType.RECURRING:
            count = self.recurring_interval_count or 1
            interval = self.get_recurring_interval_display()
            every = interval if count == 1 else f"{count} {interval}s"
            return f"{self._money(self.price)} per {every}"

        amount = self._money(self.price)
        if self.base_price is not None:
            amount = f"{self._money(self.base_price)} base + {amount} {self.unit_phrase}"
        elif self.unit_of_measure != self.UnitOfMeasure.PIECE or self.unit_label:
            amount = f"{amount} {self.unit_phrase}"

        if self.pricing_type == self.PricingType.FROM:
            return f"from {amount}"
        return amount

    # --- lifecycle ------------------------------------------------------------

    def soft_delete(self, user=None):
        """Remove the variant, handing the default flag on if it held it.

        An item whose only default variant is deleted would otherwise have no
        default, and every "what does this cost?" would come back empty. Skipped when
        the parent item is going too — promoting a default on a dying item is churn,
        and the cascade in ``CatalogItem.soft_delete`` deletes the promoted row a
        moment later anyway.
        """
        was_default = self.is_default

        # Reimplemented rather than delegated because the base `soft_delete` names
        # its update_fields, and `is_default` is not among them — the same reason
        # EventTemplate and KBWebsite reimplement it to mangle their unique columns.
        # Clearing the flag on the way out is what lets the partial unique constraint
        # accept the successor promoted below.
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.is_default = False
        update_fields = ["is_deleted", "deleted_at", "is_default"]
        if user:
            self.deleted_by = user
            update_fields.append("deleted_by")
        self.save(update_fields=update_fields)

        if not was_default or self.item.is_deleted:
            return

        successor = (
            CatalogVariant.objects.filter(item_id=self.item_id)
            .exclude(pk=self.pk)
            .order_by("sort_order", "created_at")
            .first()
        )
        if successor is not None:
            successor.is_default = True
            successor.save(update_fields=["is_default"])

    def restore(self):
        """Un-delete, refusing to create a duplicate SKU or a second default."""
        if self.sku:
            clash = (
                CatalogVariant.objects.filter(
                    tenant_id=self.tenant_id, sku__iexact=self.sku
                )
                .exclude(pk=self.pk)
                .exists()
            )
            if clash:
                raise ValidationError(
                    {
                        "sku": _(
                            "Another active variant already uses this SKU. Change or "
                            "remove it before restoring this one."
                        )
                    }
                )

        # The default flag was cleared on the way out, so a restore never collides
        # with the successor that was promoted in its place.
        self.is_default = False
        super().restore()
