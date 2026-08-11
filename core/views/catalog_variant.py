from django.db import transaction
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework.decorators import action

from core.models.catalog_variant import CatalogVariant
from core.serializers.catalog_variant import (
    CatalogVariantDropdownSerializer,
    CatalogVariantListSerializer,
    CatalogVariantSerializer,
)
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet


@extend_schema(tags=["Catalog"])
@extend_schema_view(
    list=extend_schema(
        summary="List catalog variants",
        description=(
            "The pricing table. Filter with `?item=<id>` for one product's options, "
            "`?availability_status=OUT_OF_STOCK` for the restock list, or "
            "`?track_inventory=true&stock_quantity__lte=5` for low stock. `?search=` "
            "matches label, SKU, barcode and the parent item's name."
        ),
    ),
    retrieve=extend_schema(summary="Get one variant"),
    create=extend_schema(
        summary="Add a priced option to an item",
        description=(
            "Setting `is_default=true` demotes the item's current default in the same "
            "transaction — the partial unique constraint permits only one, and asking "
            "the client to unset the old one first is a race waiting to happen."
        ),
    ),
    update=extend_schema(summary="Replace a variant"),
    partial_update=extend_schema(summary="Update a variant — the repricing endpoint"),
    destroy=extend_schema(
        summary="Delete a variant (soft)",
        description=(
            "If it was the default, the next variant by sort order is promoted, so "
            "the item never ends up with no price to quote."
        ),
    ),
)
class CatalogVariantViewSet(TenantBaseViewSet):
    """
    The priceable, sellable unit — where price, SKU, unit and stock live.

    Every item has at least one. Six pricing shapes are supported (`FIXED`, `FROM`,
    `RANGE`, `RECURRING`, `RATE`, `ON_REQUEST`, `FREE`), and `base_price` cuts across
    them for call-out charges and base fares. Every combination is validated here as a
    field error and again in the database as a check constraint.

    **Nothing here decrements `stock_quantity`.** It is a number the tenant states, not
    a balance this module computes — there are no orders in this system.
    """

    queryset = CatalogVariant.objects.all()
    serializer_class = CatalogVariantSerializer
    list_serializer_class = CatalogVariantListSerializer
    dropdown_serializer_class = CatalogVariantDropdownSerializer

    filterset_fields = {
        "item": ["exact"],
        "item__item_kind": ["exact"],
        "item__category": ["exact"],
        "pricing_type": ["exact", "in"],
        "availability_status": ["exact", "in"],
        "unit_of_measure": ["exact"],
        "currency": ["exact"],
        "is_default": ["exact"],
        "track_inventory": ["exact"],
        "is_active": ["exact"],
        "stock_quantity": ["exact", "gte", "lte"],
        "price": ["exact", "gte", "lte"],
    }
    search_fields = ["variant_label", "sku", "barcode", "item__name", "item__item_code"]
    ordering_fields = [
        "variant_label",
        "sku",
        "price",
        "sort_order",
        "stock_quantity",
        "created_at",
        "updated_at",
    ]
    ordering = ["item", "sort_order"]

    # `is_sellable` and `display_price` both read the parent item, so every row would
    # otherwise be an extra query.
    select_related_fields = ["tenant", "item"]

    def perform_create(self, serializer, **kwargs):
        with transaction.atomic():
            self._demote_existing_default(serializer.validated_data)
            super().perform_create(serializer, **kwargs)

    def perform_update(self, serializer, **kwargs):
        with transaction.atomic():
            self._demote_existing_default(
                serializer.validated_data, exclude_pk=serializer.instance.pk
            )
            super().perform_update(serializer, **kwargs)

    def _demote_existing_default(self, validated_data, exclude_pk=None):
        """Clear the item's current default before a new one is written.

        Without this, `is_default=true` on a second variant raises `IntegrityError`
        from the partial unique constraint and the client sees a 500 for what is an
        ordinary edit. Demoting inside the caller's transaction means the two writes
        cannot be interleaved into a state with no default or two.
        """
        if not validated_data.get("is_default"):
            return

        item = validated_data.get("item")
        item_id = item.pk if item is not None else None
        if item_id is None and exclude_pk is not None:
            item_id = CatalogVariant.objects.get(pk=exclude_pk).item_id
        if item_id is None:
            return

        siblings = CatalogVariant.objects.filter(item_id=item_id, is_default=True)
        if exclude_pk is not None:
            siblings = siblings.exclude(pk=exclude_pk)
        siblings.update(is_default=False)

    @extend_schema(
        summary="Make this the item's default variant",
        description=(
            "The one quoted when a caller does not specify an option. Demotes the "
            "current default atomically. Idempotent."
        ),
        request=None,
        responses={200: CatalogVariantSerializer},
    )
    @action(detail=True, methods=["post"], url_path="set-default")
    def set_default(self, request, pk=None):
        variant = self.get_object()
        with transaction.atomic():
            CatalogVariant.objects.filter(
                item_id=variant.item_id, is_default=True
            ).exclude(pk=variant.pk).update(is_default=False)
            if not variant.is_default:
                variant.is_default = True
                variant.updated_by = request.user
                variant.save(update_fields=["is_default", "updated_by"])

        serializer = self.get_serializer(variant)
        return self.success(
            data=serializer.data, message="Default variant set successfully"
        )
