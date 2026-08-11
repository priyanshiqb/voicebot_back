import copy

from django.conf import settings
from django.db import transaction
from django.db.models.functions import Lower
from rest_framework import serializers

from core.models.catalog_category import CatalogCategory
from core.models.catalog_item import CatalogItem
from core.models.catalog_variant import CatalogVariant
from core.models.event_template import EventTemplate
from core.serializers.catalog_category import CatalogCategoryRefSerializer
from core.serializers.catalog_common import (
    clean_string_array,
    resolve_tenant_id,
    run_model_clean,
    validate_json_object,
)
from core.serializers.catalog_item_media import (
    CatalogItemMediaRefSerializer,
    presigned_media_url,
)
from core.serializers.catalog_variant import (
    CatalogVariantNestedWriteSerializer,
    CatalogVariantRefSerializer,
)
from core.serializers.event_template import EventTemplateRefSerializer
from tenant.common.serializers.tenant_base_model_serializer import (
    TenantBaseModelSerializer,
)
from utils.serializers.hybrid_related_field import HybridRelatedField

CATALOG_ITEM_OWN_FIELDS = [
    "item_kind",
    "category",
    "name",
    "item_code",
    "short_description",
    "description",
    "brand",
    "tags",
    "aliases",
    "fulfilment_modes",
    "attributes",
    "event_template",
    "add_on_items",
    "lead_time_minutes",
    "available_from",
    "available_until",
    "pronunciation_hint",
    "is_featured",
    "sort_order",
    "is_active",
]


class CatalogItemRefSerializer(serializers.ModelSerializer):
    """Nested reference — what `add_on_items` renders as."""

    class Meta:
        model = CatalogItem
        fields = ("id", "name", "item_kind", "item_code")
        read_only_fields = fields


class CatalogItemSerializer(TenantBaseModelSerializer):
    """
    Full item.

    **Variants may be created inline, but not updated inline.** `POST` accepts a
    `variants` array so "add this product with its three sizes" is one request; `PUT`
    and `PATCH` reject it with a pointer to `/catalog-variants/`. Inline update would
    need replace-or-upsert semantics over a priced collection, and the failure mode of
    guessing wrong there is silently deleting a price. Refusing is the honest option.

    Every item comes back with at least one variant: `create()` calls
    `ensure_default_variant()`, which adds a `Standard` ON_REQUEST row when none were
    supplied. That is what lets every consumer read the price from
    `default_variant` without first asking whether the item has variants at all.
    """

    category = HybridRelatedField(
        serializer=CatalogCategoryRefSerializer,
        queryset=CatalogCategory.objects.all(),
        required=False,
        allow_null=True,
        help_text="Optional — an item can exist before the tenant designs a taxonomy.",
    )
    event_template = HybridRelatedField(
        serializer=EventTemplateRefSerializer,
        queryset=EventTemplate.objects.all(),
        required=False,
        allow_null=True,
        help_text=(
            "Set to make a service bookable. Duration, buffers, notice and horizon are "
            "then read from the template — do not also set duration_minutes on the "
            "variants."
        ),
    )
    add_on_items = HybridRelatedField(
        serializer=CatalogItemRefSerializer,
        queryset=CatalogItem.objects.all(),
        many=True,
        required=False,
        help_text="Items with item_kind=ADD_ON that may be added on top of this one.",
    )

    # Declared explicitly so blank entries reach the validators to be dropped, rather
    # than being rejected by the ArrayField child before cleanup can run.
    tags = serializers.ListField(
        child=serializers.CharField(max_length=50, allow_blank=True), required=False
    )
    aliases = serializers.ListField(
        child=serializers.CharField(max_length=100, allow_blank=True),
        required=False,
        help_text="What callers say. ASR returns their words, not the catalog's.",
    )
    fulfilment_modes = serializers.ListField(
        child=serializers.ChoiceField(choices=CatalogItem.FulfilmentMode.choices),
        required=False,
    )

    variants = CatalogVariantNestedWriteSerializer(
        many=True,
        required=False,
        help_text="Create-only. Update variants through /catalog-variants/.",
    )
    default_variant = CatalogVariantRefSerializer(read_only=True)
    media = CatalogItemMediaRefSerializer(many=True, read_only=True)

    item_kind_label = serializers.CharField(
        source="get_item_kind_display", read_only=True
    )

    class Meta(TenantBaseModelSerializer.Meta):
        model = CatalogItem
        fields = (
            TenantBaseModelSerializer.Meta.fields
            + CATALOG_ITEM_OWN_FIELDS
            + ["variants", "default_variant", "media", "item_kind_label"]
        )
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields + (
            "default_variant",
            "media",
            "item_kind_label",
        )

    # --- field validation -----------------------------------------------------

    def validate_name(self, value):
        value = (value or "").strip()
        if len(value) < 2:
            raise serializers.ValidationError("A name needs at least 2 characters.")
        if len(value) > 200:
            raise serializers.ValidationError("At most 200 characters.")
        return value

    def validate_item_code(self, value):
        return (value or "").strip()

    def validate_brand(self, value):
        return (value or "").strip()

    def validate_short_description(self, value):
        return (value or "").strip()

    def validate_pronunciation_hint(self, value):
        return (value or "").strip()

    def validate_tags(self, value):
        return clean_string_array(
            value,
            getattr(settings, "CATALOG_MAX_TAGS_PER_ITEM", 25),
            50,
            "tags",
        )

    def validate_aliases(self, value):
        return clean_string_array(
            value,
            getattr(settings, "CATALOG_MAX_ALIASES_PER_ITEM", 25),
            100,
            "aliases",
        )

    def validate_fulfilment_modes(self, value):
        # De-duplicated but order-preserving: the first mode listed is the one a UI
        # shows first, and a tenant who put DELIVERY first meant that.
        seen, cleaned = set(), []
        for mode in value or []:
            if mode not in seen:
                seen.add(mode)
                cleaned.append(mode)
        return cleaned

    def validate_attributes(self, value):
        return validate_json_object(
            value, getattr(settings, "CATALOG_MAX_ATTRIBUTE_BYTES", 8192)
        )

    def validate_variants(self, value):
        if self.instance is not None:
            raise serializers.ValidationError(
                "Variants cannot be edited through this endpoint. Use "
                "/api/v1/core/catalog-variants/ to add, change or remove a price."
            )
        max_variants = getattr(settings, "CATALOG_MAX_VARIANTS_PER_ITEM", 100)
        if len(value) > max_variants:
            raise serializers.ValidationError(
                f"At most {max_variants} variants per item."
            )
        labels = [entry.get("variant_label", "") for entry in value]
        duplicates = {label for label in labels if labels.count(label) > 1}
        if duplicates:
            raise serializers.ValidationError(
                f"Duplicate variant labels: {', '.join(sorted(duplicates))}. Each "
                "option needs a distinct label — it is what the agent says to tell "
                "them apart."
            )
        if sum(1 for entry in value if entry.get("is_default")) > 1:
            raise serializers.ValidationError(
                "Only one variant may be the default."
            )
        return value

    # --- object validation ----------------------------------------------------

    def validate(self, attrs):
        tenant_id = resolve_tenant_id(self)

        self._assert_same_tenant(attrs, tenant_id)
        self._run_model_rules(attrs, tenant_id)
        self._validate_add_ons(attrs, tenant_id)
        self._validate_window(attrs)

        item_code = attrs.get("item_code", getattr(self.instance, "item_code", "") or "")
        if item_code and tenant_id is not None:
            self._assert_item_code_free(tenant_id, item_code)

        return attrs

    def _assert_same_tenant(self, attrs, tenant_id):
        if tenant_id is None:
            return
        for field in ("category", "event_template"):
            related = attrs.get(field)
            if related is not None and related.tenant_id != tenant_id:
                raise serializers.ValidationError(
                    {field: f"That {field.replace('_', ' ')} belongs to a different tenant."}
                )

    def _run_model_rules(self, attrs, tenant_id):
        """`CatalogItem.clean()` — the bookable-only-if-SERVICE rule and the
        cross-tenant checks, from their one implementation on the model.

        A shallow copy so a failed validation cannot half-apply the change to the live
        instance DRF is about to save.
        """
        probe = copy.copy(self.instance) if self.instance is not None else CatalogItem()
        for field, value in attrs.items():
            if field == "variants":
                continue
            if field == "add_on_items":
                # M2M — cannot be assigned before the row exists, and `clean()` does
                # not look at it. Validated separately in `_validate_add_ons`.
                continue
            setattr(probe, field, value)
        probe.tenant_id = tenant_id or getattr(probe, "tenant_id", None)
        run_model_clean(probe)

    def _validate_add_ons(self, attrs, tenant_id):
        """The rules the `m2m_changed` receiver enforces, checked up front.

        The receiver is the backstop that makes them true from any entry point, but it
        raises inside Django's `atomic(savepoint=False)` — which marks the whole
        request transaction for rollback and surfaces as a 500, not a field error. So
        the API has to reject bad add-ons *before* touching the relation.
        """
        if "add_on_items" not in attrs:
            return
        add_ons = attrs["add_on_items"]

        max_add_ons = getattr(settings, "CATALOG_MAX_ADD_ONS_PER_ITEM", 50)
        if len(add_ons) > max_add_ons:
            raise serializers.ValidationError(
                {"add_on_items": f"At most {max_add_ons} add-ons per item."}
            )

        if self.instance is not None and any(
            add_on.pk == self.instance.pk for add_on in add_ons
        ):
            raise serializers.ValidationError(
                {"add_on_items": "An item cannot be its own add-on."}
            )

        if tenant_id is not None and any(a.tenant_id != tenant_id for a in add_ons):
            raise serializers.ValidationError(
                {"add_on_items": "Add-ons must belong to the same tenant as the item."}
            )

        wrong_kind = [
            add_on.name
            for add_on in add_ons
            if add_on.item_kind != CatalogItem.ItemKind.ADD_ON
        ]
        if wrong_kind:
            raise serializers.ValidationError(
                {
                    "add_on_items": (
                        "Only items with item_kind=ADD_ON can be attached as add-ons. "
                        f"Rejected: {', '.join(wrong_kind)}. An add-on is picked 0..n "
                        "times on top of an item; a variant is picked exactly once "
                        "instead of it."
                    )
                }
            )

    def _validate_window(self, attrs):
        available_from = attrs.get(
            "available_from", getattr(self.instance, "available_from", None)
        )
        available_until = attrs.get(
            "available_until", getattr(self.instance, "available_until", None)
        )
        if available_from and available_until and available_until < available_from:
            raise serializers.ValidationError(
                {"available_until": "The window must not end before it starts."}
            )

    def _assert_item_code_free(self, tenant_id, item_code):
        """Duplicate code as a 400, not an IntegrityError. Case-insensitive, to agree
        with the `Lower()` partial unique constraint."""
        clash = (
            CatalogItem.objects.annotate(lowered=Lower("item_code"))
            .filter(tenant_id=tenant_id, lowered=item_code.casefold())
            .exclude(item_code="")
        )
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(
                {"item_code": f"Another active item already uses the code '{item_code}'."}
            )

    # --- write ----------------------------------------------------------------

    @transaction.atomic
    def create(self, validated_data):
        """Create the item, then its variants, then guarantee a default.

        Order matters and the transaction is load-bearing. `ensure_default_variant()`
        runs last, after the supplied variants exist, so it promotes one of them rather
        than adding a junk `Standard` row alongside them — the exact bug that made a
        `post_save` signal the wrong place for it.
        """
        variants = validated_data.pop("variants", [])
        add_ons = validated_data.pop("add_on_items", None)

        item = CatalogItem.objects.create(**validated_data)

        if add_ons is not None:
            item.add_on_items.set(add_ons)

        for variant_data in variants:
            CatalogVariant.objects.create(
                tenant_id=item.tenant_id,
                item=item,
                # Copied from the item rather than read off the request: nested
                # variants are created here, not through `perform_create`, so the
                # viewset's audit injection never sees them.
                created_by=item.created_by,
                updated_by=item.updated_by,
                **variant_data,
            )

        item.ensure_default_variant()
        return item

    @transaction.atomic
    def update(self, instance, validated_data):
        # `validate_variants` already rejects the field on update; popping it keeps
        # `super().update()` from trying to assign a reverse relation if that guard is
        # ever relaxed.
        validated_data.pop("variants", None)
        return super().update(instance, validated_data)


class CatalogItemListSerializer(serializers.ModelSerializer):
    """Compact row for catalog tables.

    Carries `price` as the default variant's rendered string rather than a number:
    a list showing "8.4% per annum" next to "₹50 base + ₹18 per kilometre" cannot be
    built from an amount and a currency code.
    """

    category_name = serializers.CharField(
        source="category.name", read_only=True, default=None
    )
    item_kind_label = serializers.CharField(
        source="get_item_kind_display", read_only=True
    )
    price = serializers.SerializerMethodField()
    variant_count = serializers.SerializerMethodField()
    primary_image_url = serializers.SerializerMethodField()

    class Meta:
        model = CatalogItem
        fields = (
            "id",
            "name",
            "item_code",
            "item_kind",
            "item_kind_label",
            "category",
            "category_name",
            "brand",
            "short_description",
            "tags",
            "price",
            "variant_count",
            "primary_image_url",
            "is_featured",
            "is_active",
            "sort_order",
            "created_at",
        )
        read_only_fields = fields

    def get_price(self, obj) -> str | None:
        variant = obj.default_variant
        return variant.display_price if variant else None

    def get_variant_count(self, obj) -> int:
        return obj.variants.count()

    def get_primary_image_url(self, obj) -> str | None:
        primary = next(
            (
                media
                for media in obj.media.all()
                if media.is_primary
                and media.media_type == media.MediaType.IMAGE
                and media.upload_status == media.UploadStatus.CONFIRMED
            ),
            None,
        )
        return presigned_media_url(primary) if primary is not None else None


class CatalogItemDropdownSerializer(serializers.ModelSerializer):
    """`label`/`value` pairs, matching the shape every other dropdown endpoint uses."""

    label = serializers.SerializerMethodField()
    value = serializers.UUIDField(source="id", read_only=True)

    class Meta:
        model = CatalogItem
        fields = ("label", "value")

    def get_label(self, obj) -> str:
        return f"{obj.name} ({obj.item_code})" if obj.item_code else obj.name


class CatalogItemLookupSerializer(serializers.ModelSerializer):
    """What the voice runtime reads.

    Shaped for one job: the caller said something, and the agent needs to know what it
    is, whether it can be sold, and what to quote. Everything a bot would not say out
    loud is left out — audit fields, sort order, timestamps.

    `price` is read live rather than from an embedding, which is the whole reason this
    is a structured lookup and not RAG content: a confidently stale price is worse than
    no answer.
    """

    price = serializers.SerializerMethodField()
    is_sellable = serializers.SerializerMethodField()
    variants = CatalogVariantRefSerializer(many=True, read_only=True)
    add_ons = CatalogItemRefSerializer(
        source="add_on_items", many=True, read_only=True
    )

    class Meta:
        model = CatalogItem
        fields = (
            "id",
            "name",
            "item_kind",
            "brand",
            "short_description",
            "aliases",
            "pronunciation_hint",
            "fulfilment_modes",
            "lead_time_minutes",
            "attributes",
            "price",
            "is_sellable",
            "variants",
            "add_ons",
        )
        read_only_fields = fields

    def get_price(self, obj) -> str | None:
        variant = obj.default_variant
        return variant.display_price if variant else None

    def get_is_sellable(self, obj) -> bool:
        return any(variant.is_sellable for variant in obj.variants.all())
