import copy

from django.db.models.functions import Lower
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from core.models.catalog_category import MAX_CATEGORY_DEPTH, CatalogCategory
from core.serializers.catalog_common import resolve_tenant_id, run_model_clean
from tenant.common.serializers.tenant_base_model_serializer import (
    TenantBaseModelSerializer,
)
from utils.serializers.hybrid_related_field import HybridRelatedField


def _annotated_or_count(obj, attribute, related_manager):
    """Prefer a queryset annotation, falling back to a live count.

    `getattr(...) or ...` is wrong here and was the original bug: an annotation of
    **0** is falsy, so every empty category fell through to a per-row query — exactly
    the N+1 the annotation was added to prevent, and invisible because the numbers
    were still correct.
    """
    annotated = getattr(obj, attribute, None)
    if annotated is not None:
        return annotated
    return related_manager.count()


CATALOG_CATEGORY_FIELDS = [
    "name",
    "parent",
    "depth",
    "description",
    "sort_order",
    "item_count",
    "child_count",
    "is_active",
]


class CatalogCategoryRefSerializer(serializers.ModelSerializer):
    """Nested reference — what an item's `category` renders as."""

    class Meta:
        model = CatalogCategory
        fields = ("id", "name", "depth")
        read_only_fields = fields


class CatalogCategorySerializer(TenantBaseModelSerializer):
    """
    Full category.

    `depth` is derived from `parent` on save and exposed read-only. Accepting it would
    let a client claim a depth the tree does not support, and the check constraint
    would then reject the write with an `IntegrityError` rather than a field error.
    """

    parent = HybridRelatedField(
        serializer=CatalogCategoryRefSerializer,
        queryset=CatalogCategory.objects.all(),
        required=False,
        allow_null=True,
        help_text="Parent section. Omit or send null for a root category.",
    )
    item_count = serializers.SerializerMethodField(
        help_text="Active items filed directly under this section — not its children's."
    )
    child_count = serializers.SerializerMethodField()

    class Meta(TenantBaseModelSerializer.Meta):
        model = CatalogCategory
        fields = TenantBaseModelSerializer.Meta.fields + CATALOG_CATEGORY_FIELDS
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields + (
            "depth",
            "item_count",
            "child_count",
        )

    def get_item_count(self, obj) -> int:
        return _annotated_or_count(obj, "annotated_item_count", obj.items)

    def get_child_count(self, obj) -> int:
        return _annotated_or_count(obj, "annotated_child_count", obj.children)

    def validate_name(self, value):
        value = (value or "").strip()
        if len(value) < 2:
            raise serializers.ValidationError(
                "A category name needs at least 2 characters."
            )
        if len(value) > 150:
            raise serializers.ValidationError("At most 150 characters.")
        return value

    def validate(self, attrs):
        tenant_id = resolve_tenant_id(self)

        parent = attrs["parent"] if "parent" in attrs else getattr(
            self.instance, "parent", None
        )
        name = attrs.get("name") or getattr(self.instance, "name", "")

        if parent is not None and tenant_id is not None:
            # Checked here as well as in `clean()` below so the message names the
            # field. Without a tenant match a client could graft its section under a
            # stranger's tree by supplying an id.
            if parent.tenant_id != tenant_id:
                raise serializers.ValidationError(
                    {"parent": "That category belongs to a different tenant."}
                )

        # Cycle, depth and subtree-height rules, from the one implementation on the
        # model. A shallow copy rather than `self.instance` itself: the instance is the
        # live object DRF will save, and mutating it here would half-apply the change
        # on a validation failure. The copy shares the pk, which is what
        # `subtree_height()` needs to find descendants.
        probe = copy.copy(self.instance) if self.instance is not None else CatalogCategory()
        probe.parent = parent
        probe.name = name
        # Falling back to the parent's tenant when the caller's is unknown keeps the
        # cross-tenant check a no-op instead of a false positive, while still running
        # the depth and cycle rules — which are the ones worth having either way.
        probe.tenant_id = tenant_id or getattr(parent, "tenant_id", None)
        run_model_clean(probe)

        if tenant_id is not None and name:
            self._assert_name_free(tenant_id, parent, name)

        return attrs

    def _assert_name_free(self, tenant_id, parent, name):
        """Reject a duplicate sibling name with a field error rather than a 500.

        The two partial unique constraints are the real guarantee — they hold against
        races this cannot see. Matched case-insensitively via `Lower()` so the check
        agrees with the constraint; comparing raw values here would pass "Wine" against
        an existing "wine" and then fail in the database.
        """
        clash = (
            CatalogCategory.objects.annotate(lowered=Lower("name"))
            .filter(tenant_id=tenant_id, lowered=name.casefold(), parent=parent)
        )
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            where = f"under '{parent.name}'" if parent is not None else "at the top level"
            raise serializers.ValidationError(
                {"name": f"A category called '{name}' already exists {where}."}
            )


class CatalogCategoryListSerializer(serializers.ModelSerializer):
    """Compact row.

    Deliberately not the full serializer: a category list is rendered on every catalog
    screen and the audit block is three user joins per row that nothing displays.
    """

    parent_name = serializers.CharField(
        source="parent.name", read_only=True, default=None
    )
    item_count = serializers.SerializerMethodField()

    class Meta:
        model = CatalogCategory
        fields = (
            "id",
            "name",
            "parent",
            "parent_name",
            "depth",
            "sort_order",
            "item_count",
            "is_active",
            "created_at",
        )
        read_only_fields = fields

    def get_item_count(self, obj) -> int:
        return _annotated_or_count(obj, "annotated_item_count", obj.items)


class CatalogCategoryDropdownSerializer(serializers.ModelSerializer):
    """`label`/`value` pairs, with nesting shown in the label.

    The indent is in the label rather than left to the client because a flat picker of
    section names is ambiguous the moment two parents both have a "Red".
    """

    label = serializers.SerializerMethodField()
    value = serializers.UUIDField(source="id", read_only=True)

    class Meta:
        model = CatalogCategory
        fields = ("label", "value")

    def get_label(self, obj) -> str:
        return f"{'— ' * obj.depth}{obj.name}"


class CatalogCategoryTreeSerializer(serializers.ModelSerializer):
    """The whole tree, nested, in one response.

    Exists because the alternative is the client issuing one request per level and
    reassembling it. `children` recurses at most `MAX_CATEGORY_DEPTH` levels — the
    depth cap is what makes an unbounded-looking recursion safe here.
    """

    # Self-referential, so the schema generator cannot infer the shape. Declared
    # explicitly rather than left to default to a string in the OpenAPI document.
    children = serializers.SerializerMethodField()
    item_count = serializers.SerializerMethodField()

    class Meta:
        model = CatalogCategory
        fields = (
            "id",
            "name",
            "depth",
            "sort_order",
            "is_active",
            "item_count",
            "children",
        )
        read_only_fields = fields

    def get_item_count(self, obj) -> int:
        return obj.items.count()

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_children(self, obj):
        if obj.depth >= MAX_CATEGORY_DEPTH - 1:
            return []
        children = obj.children.all().order_by("sort_order", "name")
        return CatalogCategoryTreeSerializer(
            children, many=True, context=self.context
        ).data
