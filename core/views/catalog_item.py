import django_filters
from django.db.models import Q
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.decorators import action

from core.models.catalog_item import CatalogItem
from core.models.catalog_item_media import CatalogItemMedia
from core.serializers.catalog_item import (
    CatalogItemDropdownSerializer,
    CatalogItemListSerializer,
    CatalogItemLookupSerializer,
    CatalogItemSerializer,
)
from core.serializers.catalog_item_media import CatalogItemMediaListSerializer
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet

#: How many lookup matches to return. The voice runtime reads these out loud, and a
#: bot listing fifteen options is a bot the caller hangs up on.
LOOKUP_RESULT_LIMIT = 10


class CatalogItemFilter(django_filters.FilterSet):
    """
    `tags` and `aliases` are ArrayFields. django-filter cannot auto-generate filters
    for them and **raises at filterset build time** if they are left in
    `filterset_fields` — the same trap `ContactFilter` documents.

    Both get an ANY and an ALL variant, because they answer different questions:
    "anything tagged organic or vegan" is a browse, "tagged organic AND bestseller" is
    a report.
    """

    tags = django_filters.CharFilter(
        method="filter_tags_any",
        help_text="Comma-separated. Items carrying ANY of these tags.",
    )
    tags_all = django_filters.CharFilter(
        method="filter_tags_all",
        help_text="Comma-separated. Items carrying ALL of these tags.",
    )
    alias = django_filters.CharFilter(
        method="filter_alias",
        help_text="Exact alias match — what the voice runtime resolves against.",
    )
    has_media = django_filters.BooleanFilter(
        method="filter_has_media",
        help_text="Items with at least one confirmed media file.",
    )
    price_min = django_filters.NumberFilter(
        field_name="variants__price",
        lookup_expr="gte",
        help_text="Any variant priced at or above this.",
    )
    price_max = django_filters.NumberFilter(
        field_name="variants__price",
        lookup_expr="lte",
        help_text="Any variant priced at or below this.",
    )

    class Meta:
        model = CatalogItem
        fields = [
            "item_kind",
            "category",
            "brand",
            "is_featured",
            "is_active",
            "event_template",
        ]

    @staticmethod
    def _split(value):
        return [part.strip() for part in (value or "").split(",") if part.strip()]

    def filter_tags_any(self, queryset, name, value):
        tags = self._split(value)
        return queryset.filter(tags__overlap=tags) if tags else queryset

    def filter_tags_all(self, queryset, name, value):
        tags = self._split(value)
        return queryset.filter(tags__contains=tags) if tags else queryset

    def filter_alias(self, queryset, name, value):
        value = (value or "").strip()
        return queryset.filter(aliases__contains=[value]) if value else queryset

    def filter_has_media(self, queryset, name, value):
        if value is None:
            return queryset
        confirmed = Q(
            media__upload_status=CatalogItemMedia.UploadStatus.CONFIRMED,
            media__is_deleted=False,
        )
        matching = CatalogItem.objects.filter(confirmed).values("pk")
        return (
            queryset.filter(pk__in=matching)
            if value
            else queryset.exclude(pk__in=matching)
        )


@extend_schema(tags=["Catalog"])
@extend_schema_view(
    list=extend_schema(
        summary="List catalog items",
        description=(
            "Filter with `?item_kind=`, `?category=`, `?brand=`, `?is_featured=`, "
            "`?is_active=`, `?tags=organic,vegan` (ANY), `?tags_all=organic,bestseller` "
            "(ALL), `?alias=`, `?has_media=`, `?price_min=` / `?price_max=`. "
            "`?search=` matches name, item code, brand, short description and "
            "description. Rows carry the default variant's rendered price."
        ),
    ),
    retrieve=extend_schema(
        summary="Get one item",
        description="Includes its variants, media, add-ons and default variant.",
    ),
    create=extend_schema(
        summary="Create an item",
        description=(
            "A `variants` array may be supplied inline — one request for "
            "'add this product with its three sizes'. If none is given, a `Standard` "
            "variant priced ON_REQUEST is created, so every item always has a price "
            "to quote from."
        ),
    ),
    update=extend_schema(summary="Replace an item"),
    partial_update=extend_schema(
        summary="Update an item",
        description=(
            "`variants` is rejected here — add, change or remove prices through "
            "`/catalog-variants/`."
        ),
    ),
    destroy=extend_schema(
        summary="Delete an item (soft)",
        description="Cascades to its variants and media, so nothing outlives it.",
    ),
)
class CatalogItemViewSet(TenantBaseViewSet):
    """
    Everything the tenant sells — goods, services, bundles and add-ons.

    Price and stock are **not** here; they live on `/catalog-variants/`, because most
    real catalog lines have several prices under one name. Reads of this endpoint
    carry `default_variant` so the common "what does this cost?" needs no second call.
    """

    queryset = CatalogItem.objects.all()
    serializer_class = CatalogItemSerializer
    list_serializer_class = CatalogItemListSerializer
    dropdown_serializer_class = CatalogItemDropdownSerializer

    filterset_class = CatalogItemFilter
    search_fields = ["name", "item_code", "brand", "short_description", "description"]
    ordering_fields = [
        "name",
        "brand",
        "sort_order",
        "item_kind",
        "created_at",
        "updated_at",
    ]
    ordering = ["sort_order", "-created_at"]

    select_related_fields = ["tenant", "category", "event_template"]
    # `variants` and `media` are rendered on both list and detail. Without prefetching
    # them the list is two extra queries per row, and the default-variant lookup a
    # third.
    prefetch_related_fields = ["variants", "media", "add_on_items"]

    @extend_schema(
        summary="Resolve what a caller said to catalog items",
        description=(
            "The voice runtime's entry point. Matches `q` against the item name, its "
            "aliases, its brand and its variants' SKUs — aliases first, because "
            "speech recognition returns the caller's words ('coke'), not the "
            "catalog's ('Coca-Cola 500 ml').\n\n"
            "Prices are read live from the variant rows, never from an embedding: a "
            "confidently stale price is worse than no answer.\n\n"
            f"Returns at most {LOOKUP_RESULT_LIMIT} matches, sellable ones first."
        ),
        parameters=[
            OpenApiParameter(
                name="q",
                description="What the caller said. Required, at least 2 characters.",
                required=True,
                type=str,
            ),
            OpenApiParameter(
                name="sellable_only",
                description="Drop items with nothing sellable. Default false.",
                required=False,
                type=bool,
            ),
        ],
        responses={200: CatalogItemLookupSerializer(many=True)},
    )
    @action(detail=False, methods=["get"])
    def lookup(self, request):
        query = (request.query_params.get("q") or "").strip()
        if len(query) < 2:
            return self.error(
                message="Pass ?q= with at least 2 characters.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        # No extra prefetch here. `prefetch_related_fields` on the class already
        # covers variants, media and add-ons, and adding a `Prefetch("variants", …)`
        # on top raises "'variants' lookup was already seen with a different
        # queryset". The ordering a Prefetch would impose is already the default —
        # `CatalogVariant.Meta.ordering` is `["sort_order", "created_at"]`.
        queryset = self.get_queryset().filter(is_active=True)

        # Ordered deliberately rather than OR'd into one query: an alias hit is a
        # stronger signal than a substring of a description, and the caller hears only
        # the first few results.
        matched, seen = [], set()
        for lookup_filter in (
            {"aliases__contains": [query]},
            {"name__iexact": query},
            {"name__icontains": query},
            {"variants__sku__iexact": query},
            {"brand__icontains": query},
        ):
            for item in queryset.filter(**lookup_filter):
                if item.pk in seen:
                    continue
                seen.add(item.pk)
                matched.append(item)
            if len(matched) >= LOOKUP_RESULT_LIMIT:
                break

        if request.query_params.get("sellable_only", "").lower() in ("1", "true", "yes"):
            matched = [
                item
                for item in matched
                if any(variant.is_sellable for variant in item.variants.all())
            ]

        serializer = CatalogItemLookupSerializer(
            matched[:LOOKUP_RESULT_LIMIT],
            many=True,
            context=self.get_serializer_context(),
        )
        return self.success(
            data=serializer.data,
            message=f"{len(serializer.data)} match(es) for '{query}'",
        )

    @extend_schema(
        summary="Distinct tags in use, with counts",
        description=(
            "Powers the tag filter without the client paging the whole catalog to "
            "discover which tags exist."
        ),
    )
    @action(detail=False, methods=["get"])
    def tags(self, request):
        counts = {}
        for tag_list in self.get_queryset().values_list("tags", flat=True):
            for tag in tag_list or []:
                counts[tag] = counts.get(tag, 0) + 1

        data = [
            {"tag": tag, "count": count}
            for tag, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        return self.success(data=data, message="Tags retrieved successfully")

    @extend_schema(
        summary="Media for one item",
        responses={200: None},
    )
    @action(detail=True, methods=["get"])
    def media(self, request, pk=None):
        """Confirmed media only.

        A PENDING row points at a key that may have nothing behind it — the client
        requested an upload URL and never finished. Serving those produces broken
        images with presigned URLs that 404.
        """
        item = self.get_object()
        queryset = item.media.filter(
            upload_status=CatalogItemMedia.UploadStatus.CONFIRMED
        ).order_by("sort_order", "created_at")
        serializer = CatalogItemMediaListSerializer(
            queryset, many=True, context=self.get_serializer_context()
        )
        return self.success(data=serializer.data, message="Media retrieved successfully")
