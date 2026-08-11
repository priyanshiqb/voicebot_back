import copy

from django.conf import settings
from django.db.models.functions import Lower
from rest_framework import serializers

from core.models.catalog_item import CatalogItem
from core.models.catalog_variant import CatalogVariant
from core.serializers.catalog_common import (
    normalize_currency,
    resolve_tenant_id,
    run_model_clean,
    validate_json_object,
    validate_variant_pricing,
)
from tenant.common.serializers.tenant_base_model_serializer import (
    TenantBaseModelSerializer,
)

#: Everything except the relation to the item and the audit block. Shared with the
#: nested write serializer used by item create, so the two cannot drift.
CATALOG_VARIANT_OWN_FIELDS = [
    "variant_label",
    "options",
    "sku",
    "barcode",
    "is_default",
    "sort_order",
    "pricing_type",
    "price",
    "base_price",
    "max_price",
    "compare_at_price",
    "currency",
    "unit_of_measure",
    "unit_label",
    "recurring_interval",
    "recurring_interval_count",
    "deposit_amount",
    "duration_minutes",
    "tax_rate_percentage",
    "is_tax_inclusive",
    "tax_code",
    "availability_status",
    "track_inventory",
    "stock_quantity",
    "low_stock_threshold",
    "min_order_quantity",
    "quantity_increment",
    "is_active",
]

#: Read-only computed fields every variant representation carries.
CATALOG_VARIANT_DERIVED_FIELDS = [
    "display_price",
    "unit_phrase",
    "is_quotable",
    "is_sellable",
]


class CatalogVariantRefSerializer(serializers.ModelSerializer):
    """Nested reference — what an item's `variants` and `default_variant` render as.

    Carries `display_price` rather than the raw amount: a caller rendering a variant
    reference wants the sentence, and it is the model's job to compose it so voice and
    dashboard cannot disagree about how "₹50 base + ₹18 per kilometre" reads.
    """

    display_price = serializers.CharField(read_only=True)

    class Meta:
        model = CatalogVariant
        fields = ("id", "variant_label", "sku", "display_price", "is_default")
        read_only_fields = fields


class _VariantValidationMixin:
    """The validation shared by the standalone and nested variant serializers.

    Split out rather than duplicated because the nested serializer has no `item` field
    — the parent supplies it — but needs every other rule identically. Two copies of
    ten pricing rules is two copies that drift.
    """

    def _target_item(self, attrs):
        """The item this variant will belong to, however it arrives.

        Three routes: named in the payload (standalone create), already on the
        instance (update), or handed down by the parent item serializer (nested
        create). Returning `None` means the item-dependent rules are skipped.
        """
        if "item" in attrs:
            return attrs["item"]
        if self.instance is not None:
            return self.instance.item
        return self.context.get("catalog_item")

    def validate_variant_label(self, value):
        """Strip only.

        No blank check here: DRF's `CharField` trims whitespace and rejects the empty
        result before this method is reached, so a hand-written "a label is required"
        would be unreachable code. Omitting the field entirely falls back to the
        model's `"Standard"` default, which is the intended behaviour for
        single-option items.
        """
        return (value or "").strip()

    def validate_sku(self, value):
        return (value or "").strip()

    def validate_barcode(self, value):
        return (value or "").strip()

    def validate_currency(self, value):
        return normalize_currency(value)

    def validate_options(self, value):
        return validate_json_object(
            value,
            getattr(settings, "CATALOG_MAX_ATTRIBUTE_BYTES", 8192),
            label="options",
        )

    def validate(self, attrs):
        # Every pricing CHECK constraint, mirrored as a field error.
        validate_variant_pricing(attrs, instance=self.instance)

        tenant_id = resolve_tenant_id(self) or getattr(
            self.context.get("catalog_item"), "tenant_id", None
        )
        item = self._target_item(attrs)

        if item is not None and tenant_id is not None and item.tenant_id != tenant_id:
            raise serializers.ValidationError(
                {"item": "That item belongs to a different tenant."}
            )

        if item is not None:
            self._run_model_rules(attrs, item, tenant_id)
            self._assert_variant_cap(item)

        sku = attrs.get("sku", getattr(self.instance, "sku", "") or "")
        if sku and tenant_id is not None:
            self._assert_sku_free(tenant_id, sku)

        return attrs

    def _run_model_rules(self, attrs, item, tenant_id):
        """`CatalogVariant.clean()`, from its one implementation on the model.

        Carries the duration-vs-event-template rule and the stock-vs-tracking rule.
        DRF never calls `full_clean()`, so without this they hold in the admin and the
        shell but not over the API.
        """
        probe = copy.copy(self.instance) if self.instance is not None else CatalogVariant()
        for field, value in attrs.items():
            setattr(probe, field, value)
        probe.item = item
        probe.tenant_id = tenant_id or item.tenant_id
        run_model_clean(probe)

    def _assert_variant_cap(self, item):
        """Bound the number of variants per item.

        Not vanity: `display_price` and the item detail response render every variant,
        and someone modelling a hotel as one row per date — the thing §3.1 gap 4 says
        not to do — would otherwise get a few hundred of them before noticing.
        """
        if self.instance is not None:
            return
        max_variants = getattr(settings, "CATALOG_MAX_VARIANTS_PER_ITEM", 100)
        existing = CatalogVariant.objects.filter(item_id=item.pk).count()
        if existing >= max_variants:
            raise serializers.ValidationError(
                {
                    "item": (
                        f"'{item.name}' already has {existing} variants; the maximum is "
                        f"{max_variants}. If you are modelling one row per date, that "
                        "is not what variants are for."
                    )
                }
            )

    def _assert_sku_free(self, tenant_id, sku):
        """Duplicate SKU as a 400 on `sku`, not an IntegrityError.

        Case-insensitive via `Lower()` so the check agrees with the partial unique
        constraint; a raw comparison would pass "ABC-1" against an existing "abc-1"
        and then fail in the database.
        """
        clash = (
            CatalogVariant.objects.annotate(lowered=Lower("sku"))
            .filter(tenant_id=tenant_id, lowered=sku.casefold())
            .exclude(sku="")
        )
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(
                {"sku": f"Another active variant already uses the SKU '{sku}'."}
            )


class CatalogVariantNestedWriteSerializer(
    _VariantValidationMixin, serializers.ModelSerializer
):
    """Variants supplied inline when an item is created.

    No `item`, `tenant` or audit fields — the parent owns those. Used only by
    `CatalogItemSerializer.create()`, which passes the item down through the
    serializer context so the item-dependent rules still run.
    """

    display_price = serializers.CharField(read_only=True)

    class Meta:
        model = CatalogVariant
        fields = ["id"] + CATALOG_VARIANT_OWN_FIELDS + ["display_price"]
        read_only_fields = ("id", "display_price")


class CatalogVariantSerializer(_VariantValidationMixin, TenantBaseModelSerializer):
    """
    Full variant — the priceable unit.

    `item` is a plain primary-key relation with the item's name and kind alongside it,
    rather than a nested `HybridRelatedField`. Nesting would need
    `CatalogItemRefSerializer`, and `catalog_item` already imports this module for its
    inline `variants` — the import would be a cycle. A variant row does not need the
    whole item anyway; the two flat labels are what a client renders.
    """

    item = serializers.PrimaryKeyRelatedField(
        queryset=CatalogItem.objects.all(),
        help_text="The item this is a priced option of.",
    )
    item_name = serializers.CharField(source="item.name", read_only=True)
    item_kind = serializers.CharField(source="item.item_kind", read_only=True)

    display_price = serializers.CharField(
        read_only=True,
        help_text="Rendered price, composed on the model so every consumer agrees.",
    )
    unit_phrase = serializers.CharField(read_only=True)
    is_quotable = serializers.BooleanField(read_only=True)
    is_sellable = serializers.BooleanField(
        read_only=True,
        help_text=(
            "The single definition of 'can we sell this right now?' — folds in the "
            "variant's status and stock and the parent item's active flag and season."
        ),
    )

    pricing_type_label = serializers.CharField(
        source="get_pricing_type_display", read_only=True
    )
    availability_status_label = serializers.CharField(
        source="get_availability_status_display", read_only=True
    )

    class Meta(TenantBaseModelSerializer.Meta):
        model = CatalogVariant
        fields = (
            TenantBaseModelSerializer.Meta.fields
            + ["item", "item_name", "item_kind"]
            + CATALOG_VARIANT_OWN_FIELDS
            + CATALOG_VARIANT_DERIVED_FIELDS
            + ["pricing_type_label", "availability_status_label"]
        )
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields + tuple(
            CATALOG_VARIANT_DERIVED_FIELDS
            + ["item_name", "item_kind", "pricing_type_label", "availability_status_label"]
        )


class CatalogVariantListSerializer(serializers.ModelSerializer):
    """Compact row for pricing tables and stock screens.

    Drops the audit block — three user joins per row that no pricing table renders —
    but keeps `display_price` and `is_sellable`, which are the two columns the screen
    exists for.
    """

    item_name = serializers.CharField(source="item.name", read_only=True)
    display_price = serializers.CharField(read_only=True)
    is_sellable = serializers.BooleanField(read_only=True)

    class Meta:
        model = CatalogVariant
        fields = (
            "id",
            "item",
            "item_name",
            "variant_label",
            "sku",
            "is_default",
            "pricing_type",
            "price",
            "currency",
            "unit_of_measure",
            "display_price",
            "availability_status",
            "track_inventory",
            "stock_quantity",
            "is_sellable",
            "is_active",
            "created_at",
        )
        read_only_fields = fields


class CatalogVariantDropdownSerializer(serializers.ModelSerializer):
    """`label`/`value` pairs.

    The label carries the price because that is what disambiguates two options with
    the same name across different items — and it is what a picker is being used to
    choose between.
    """

    label = serializers.SerializerMethodField()
    value = serializers.UUIDField(source="id", read_only=True)

    class Meta:
        model = CatalogVariant
        fields = ("label", "value")

    def get_label(self, obj) -> str:
        return f"{obj.variant_label} — {obj.display_price}"
