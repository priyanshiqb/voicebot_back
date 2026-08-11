from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Count, Q
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.decorators import action

from core.models.catalog_category import CatalogCategory
from core.serializers.catalog_category import (
    CatalogCategoryDropdownSerializer,
    CatalogCategoryListSerializer,
    CatalogCategorySerializer,
    CatalogCategoryTreeSerializer,
)
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet


@extend_schema(tags=["Catalog"])
@extend_schema_view(
    list=extend_schema(
        summary="List catalog categories",
        description=(
            "Flat list, ordered by depth then `sort_order`. Use `?parent=<id>` for one "
            "level, `?parent__isnull=true` for the roots, or `/tree/` for the whole "
            "nested structure in one response."
        ),
    ),
    retrieve=extend_schema(summary="Get one category"),
    create=extend_schema(summary="Create a category"),
    update=extend_schema(summary="Replace a category"),
    partial_update=extend_schema(summary="Update a category"),
    destroy=extend_schema(
        summary="Delete a category (soft)",
        description=(
            "Refused with 400 while the section still contains sub-categories or "
            "items. Move or delete those first, or set `is_active=false` to hide the "
            "section while keeping its contents."
        ),
    ),
)
class CatalogCategoryViewSet(TenantBaseViewSet):
    """
    The tenant's navigation tree over their catalog.

    Nesting is capped at 3 levels — deep enough for "Menu → Starters → Vegetarian"
    and shallow enough that the whole tree is one query.
    """

    queryset = CatalogCategory.objects.all()
    serializer_class = CatalogCategorySerializer
    list_serializer_class = CatalogCategoryListSerializer
    dropdown_serializer_class = CatalogCategoryDropdownSerializer

    # Dict form, not a list: a list generates only the `exact` lookup, so the
    # documented `?parent__isnull=true` — how a client asks for the roots — was
    # silently ignored rather than rejected, and returned the whole tree.
    filterset_fields = {
        "parent": ["exact", "isnull"],
        "depth": ["exact", "gte", "lte"],
        "is_active": ["exact"],
    }
    search_fields = ["name", "description"]
    ordering_fields = ["name", "sort_order", "depth", "created_at", "updated_at"]
    # Tree order, not recency: a category list is browsed, and "most recently added"
    # puts a leaf above the root it belongs to.
    ordering = ["depth", "sort_order", "name"]

    select_related_fields = ["tenant", "parent"]

    def get_queryset(self):
        """Annotate the two counts every list row shows.

        Without this the list issues two extra queries per row — the N+1 that turns a
        50-category page into 101 queries. The serializer prefers the annotation and
        falls back to a live count for single-object reads.
        """
        return super().get_queryset().annotate(
            annotated_item_count=Count(
                "items", filter=Q(items__is_deleted=False), distinct=True
            ),
            annotated_child_count=Count(
                "children", filter=Q(children__is_deleted=False), distinct=True
            ),
        )

    def destroy(self, request, *args, **kwargs):
        """Translate the model's refusal into a 400 rather than a 500.

        `CatalogCategory.soft_delete()` raises `ValidationError` while the section is
        occupied. That is a Django exception, not a DRF one, so it would otherwise
        escape the handler as an unhandled error.
        """
        instance = self.get_object()
        try:
            self.perform_destroy(instance)
        except DjangoValidationError as exc:
            return self.error(
                message="; ".join(exc.messages),
                errors={"detail": exc.messages},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return self.success(message="Deleted successfully")

    @extend_schema(
        summary="The whole category tree, nested",
        description=(
            "Roots with their children inlined, ordered by `sort_order`. One request "
            "instead of one per level. Unpaginated — the depth cap and the per-tenant "
            "size of a catalog taxonomy make this bounded by construction."
        ),
        responses={200: CatalogCategoryTreeSerializer(many=True)},
    )
    @action(detail=False, methods=["get"])
    def tree(self, request):
        roots = (
            self.filter_queryset(self.get_queryset())
            .filter(parent__isnull=True)
            .order_by("sort_order", "name")
        )
        serializer = CatalogCategoryTreeSerializer(
            roots, many=True, context=self.get_serializer_context()
        )
        return self.success(
            data=serializer.data, message="Category tree retrieved successfully"
        )

    @extend_schema(
        summary="Delete several categories (soft)",
        description=(
            "Each is checked individually — an occupied section is reported and the "
            "rest are left alone, rather than the whole batch failing."
        ),
    )
    @action(detail=False, methods=["post"])
    def bulk_delete(self, request):
        """Replaces the inherited `bulk_delete` — same route, different semantics.

        The base implementation loops `soft_delete()` inside one transaction, so the
        first occupied category aborts every deletion in the batch, including the ones
        that would have succeeded. Here the refusal is expected and actionable, so
        each id is reported on its own.
        """
        ids = request.data.get("ids", [])
        if not isinstance(ids, list) or not ids:
            return self.error(
                message="ids must be a non-empty list",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        queryset = self.get_queryset().filter(id__in=ids)
        found = {str(obj.id): obj for obj in queryset}
        missing = [str(i) for i in ids if str(i) not in found]
        if missing:
            return self.error(
                message="Some ids are invalid",
                errors={"ids": missing},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        deleted, refused = [], {}
        for category_id, category in found.items():
            try:
                category.soft_delete(user=request.user)
                deleted.append(category_id)
            except DjangoValidationError as exc:
                refused[category_id] = "; ".join(exc.messages)

        return self.success(
            data={"deleted": deleted, "refused": refused},
            message=f"{len(deleted)} deleted, {len(refused)} refused",
        )
