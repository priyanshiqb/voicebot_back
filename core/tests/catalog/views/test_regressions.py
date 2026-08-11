"""
Named regression tests for defects already fixed once.

Each of these was found by a throwaway smoke run rather than by the suite, and each
would be re-introduced by a plausible, well-intentioned edit — putting a field back
into `filterset_fields` where it looks like it belongs, adding a `Prefetch` to control
ordering, writing a `SerializerMethodField` without a return annotation.

Two of them *would* already fail something: a broken filterset 500s every item-list
test, and a duplicate prefetch breaks `LookupTests`. But the failure would arrive as
an unexplained 500 in a test about tag filtering, and whoever hit it would have to
rediscover the cause. These tests fail with the reason attached.

The third — schema generation — had **no** automated coverage at all. It was verified
by hand with `manage.py spectacular`, which is exactly the kind of check that stops
being run.
"""

from django.test import SimpleTestCase, TestCase
from drf_spectacular.drainage import GENERATOR_STATS
from drf_spectacular.generators import SchemaGenerator

from core.models.catalog_item import CatalogItem
from core.views.catalog_category import CatalogCategoryViewSet
from core.views.catalog_item import CatalogItemViewSet
from core.views.catalog_item_media import CatalogItemMediaViewSet
from core.views.catalog_variant import CatalogVariantViewSet
from core.tests.catalog.views.base import CATEGORIES, ITEMS, MEDIA, VARIANTS, CatalogAPITestCase

CATALOG_VIEWSETS = (
    CatalogCategoryViewSet,
    CatalogItemViewSet,
    CatalogVariantViewSet,
    CatalogItemMediaViewSet,
)


class ArrayFieldFilterRegressionTests(TestCase):
    """django-filter cannot auto-generate a filter for an `ArrayField`.

    It does not degrade — it raises `AssertionError` while *building* the filterset,
    which happens on the first list request. So `tags` or `aliases` left in
    `filterset_fields` takes the whole item list down, not just that one filter.

    The tempting edit is to add `"tags"` to `filterset_fields` alongside `item_kind`,
    where it looks like it belongs. `CatalogItemFilter` exists to stop that, and this
    test explains why when someone tries.
    """

    def test_every_catalog_filterset_builds(self):
        """Instantiating is what raises. Doing it here means the failure names the
        viewset instead of arriving as a 500 in an unrelated listing test."""
        for viewset in CATALOG_VIEWSETS:
            with self.subTest(viewset=viewset.__name__):
                filterset_class = getattr(viewset, "filterset_class", None)
                if filterset_class is None:
                    continue
                filterset_class(
                    data={}, queryset=viewset.queryset.all(), request=None
                )

    def test_no_array_field_is_left_to_auto_generation(self):
        """The rule in one assertion: any ArrayField on a catalog model must be served
        by an explicit filter method, never by `Meta.fields`."""
        from django.contrib.postgres.fields import ArrayField

        for viewset in CATALOG_VIEWSETS:
            filterset_class = getattr(viewset, "filterset_class", None)
            declared = getattr(viewset, "filterset_fields", None)

            model = viewset.queryset.model
            array_field_names = {
                field.name
                for field in model._meta.get_fields()
                if isinstance(field, ArrayField)
            }
            if not array_field_names:
                continue

            with self.subTest(viewset=viewset.__name__):
                auto_generated = set()
                if filterset_class is not None:
                    auto_generated |= set(filterset_class.Meta.fields or [])
                if declared:
                    auto_generated |= set(declared)

                offenders = array_field_names & auto_generated
                self.assertEqual(
                    offenders,
                    set(),
                    f"{sorted(offenders)} are ArrayFields and cannot be "
                    f"auto-filtered — django-filter raises at filterset build time, "
                    f"taking the whole list endpoint down. Add an explicit filter "
                    f"method to {filterset_class.__name__ if filterset_class else 'a FilterSet'} instead.",
                )

    def test_the_array_filters_actually_narrow_the_queryset(self):
        """A filter that is silently ignored is worse than a missing one: the client
        believes it narrowed the list and shows the user the wrong thing."""
        tenant_items = {
            "tagged": CatalogItem.objects.create(
                tenant_id=self._tenant_id(),
                item_kind=CatalogItem.ItemKind.PHYSICAL_GOOD,
                name="Tagged",
                tags=["organic"],
                aliases=["bio"],
            ),
            "untagged": CatalogItem.objects.create(
                tenant_id=self._tenant_id(),
                item_kind=CatalogItem.ItemKind.PHYSICAL_GOOD,
                name="Untagged",
            ),
        }

        filterset_class = CatalogItemViewSet.filterset_class
        for params, expected in (
            ({"tags": "organic"}, {tenant_items["tagged"].pk}),
            ({"tags_all": "organic"}, {tenant_items["tagged"].pk}),
            ({"alias": "bio"}, {tenant_items["tagged"].pk}),
            ({"tags": "missing"}, set()),
        ):
            with self.subTest(params=params):
                filterset = filterset_class(
                    data=params, queryset=CatalogItem.objects.all(), request=None
                )
                self.assertTrue(filterset.is_valid(), filterset.errors)
                self.assertEqual({row.pk for row in filterset.qs}, expected)

    def _tenant_id(self):
        from tenant.models.Tenant import Tenant

        if not hasattr(self, "_cached_tenant"):
            self._cached_tenant = Tenant.objects.create(
                name="Acme", subdomain="acme-regression"
            )
        return self._cached_tenant.id


class DuplicatePrefetchRegressionTests(CatalogAPITestCase):
    """A second `Prefetch` for a relation the viewset already prefetches raises
    ``ValueError: 'variants' lookup was already seen with a different queryset``.

    The tempting edit is adding `Prefetch("variants", queryset=...)` inside an action
    to control ordering — which is unnecessary, because `CatalogVariant.Meta.ordering`
    already applies. The cost of getting it wrong is a 500 on the endpoint the voice
    agent calls mid-conversation.
    """

    def test_every_collection_endpoint_evaluates_its_queryset(self):
        """Queryset-construction errors only surface when the queryset is evaluated,
        which is to say at request time. Hitting each endpoint is what catches them."""
        item = self.make_item("Coca-Cola", aliases=["coke"])
        self.make_variant(item=item, is_default=True)
        self.make_category("Menu")

        endpoints = [
            CATEGORIES,
            f"{CATEGORIES}tree/",
            f"{CATEGORIES}dropdown/",
            ITEMS,
            f"{ITEMS}dropdown/",
            f"{ITEMS}tags/",
            f"{ITEMS}lookup/?q=coke",
            f"{ITEMS}{item.pk}/",
            f"{ITEMS}{item.pk}/media/",
            VARIANTS,
            f"{VARIANTS}dropdown/",
            MEDIA,
        ]
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                response = self.api.get(endpoint)
                self.assertLess(
                    response.status_code,
                    500,
                    f"{endpoint} returned {response.status_code} — a queryset that "
                    f"cannot be built surfaces here, not at import time",
                )

    def test_the_lookup_action_does_not_re_prefetch(self):
        """The specific shape of the bug: `lookup` builds on `get_queryset()`, which
        already carries `prefetch_related_fields`, so it must not add its own
        `Prefetch` for the same relation."""
        item = self.make_item("Coca-Cola", aliases=["coke"])
        self.make_variant(item=item, is_default=True)

        response = self.api.get(f"{ITEMS}lookup/", {"q": "coke"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"][0]["name"], "Coca-Cola")

    def test_prefetch_lookups_are_declared_once(self):
        """Static form of the same rule — a relation named twice in
        `prefetch_related_fields` is the other way to reach the ValueError."""
        for viewset in CATALOG_VIEWSETS:
            with self.subTest(viewset=viewset.__name__):
                lookups = list(getattr(viewset, "prefetch_related_fields", []) or [])
                self.assertEqual(
                    len(lookups), len(set(lookups)), f"duplicate prefetch in {lookups}"
                )


class SchemaGenerationRegressionTests(SimpleTestCase):
    """The OpenAPI document must generate cleanly for the catalog.

    This is the one with no coverage before now. A `SerializerMethodField` without a
    return annotation does not fail anything — drf-spectacular warns and **defaults
    the type to string**, so `item_count` ships to every client's generated SDK as a
    string and the mistake is invisible until someone tries to sort by it.

    Asserted two ways: no warnings attributable to the catalog, and the concrete types
    of the fields that were wrong. The second matters more — it survives any change to
    how drf-spectacular reports warnings.
    """

    databases = "__all__"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        GENERATOR_STATS.reset()
        with GENERATOR_STATS.silence():
            cls.schema = SchemaGenerator().get_schema(request=None, public=True)
        cls.warnings = dict(GENERATOR_STATS._warn_cache)
        cls.errors = dict(GENERATOR_STATS._error_cache)
        GENERATOR_STATS.reset()

    def test_the_schema_generates_without_errors(self):
        self.assertEqual(self.errors, {}, "schema generation reported errors")

    def test_no_warning_mentions_the_catalog(self):
        offenders = [
            message
            for message in self.warnings
            if "catalog" in message.lower() or "Catalog" in message
        ]
        self.assertEqual(
            offenders,
            [],
            "catalog serializers produced schema warnings — an unannotated "
            "SerializerMethodField silently becomes a string in every generated "
            "client:\n" + "\n".join(offenders),
        )

    def _component(self, name):
        components = self.schema["components"]["schemas"]
        # Short message on purpose: the failure is "this component is missing", and
        # dumping every schema in the project buries it under 800 KB of noise.
        near = [key for key in components if key.startswith("Catalog")]
        self.assertIn(name, components, f"{name} missing; catalog components: {near}")
        return components[name]

    def test_the_count_fields_are_integers_not_strings(self):
        for component, field in (
            ("CatalogCategory", "item_count"),
            ("CatalogCategory", "child_count"),
            ("CatalogCategoryList", "item_count"),
            ("CatalogItemList", "variant_count"),
        ):
            with self.subTest(component=component, field=field):
                schema = self._component(component)["properties"][field]
                self.assertEqual(schema.get("type"), "integer", schema)

    def test_the_optional_string_fields_are_nullable_strings(self):
        """`-> str | None` has to reach the document as a nullable string. Without the
        annotation these default to a bare string, and a generated client then treats
        "no price yet" / "no image yet" as impossible."""
        for component, field in (
            ("CatalogItemMediaRef", "file_url"),
            ("CatalogItemList", "price"),
            ("CatalogItemList", "primary_image_url"),
            ("CatalogItemLookup", "price"),
        ):
            with self.subTest(component=component, field=field):
                schema = self._component(component)["properties"][field]
                self.assertEqual(schema.get("type"), "string", schema)
                self.assertTrue(schema.get("nullable"), schema)

    def test_the_recursive_children_field_is_an_array(self):
        """Self-referential, so the generator cannot infer it and defaults to string
        unless it is annotated explicitly."""
        schema = self._component("CatalogCategoryTree")["properties"]["children"]
        self.assertEqual(schema.get("type"), "array", schema)

    def test_the_boolean_and_rendered_price_fields_keep_their_types(self):
        """`is_sellable` is the field a client branches on, so it must not arrive as a
        string; `display_price` is already-rendered text and must not arrive as a
        number."""
        boolean = self._component("CatalogItemLookup")["properties"]["is_sellable"]
        self.assertEqual(boolean.get("type"), "boolean", boolean)

        rendered = self._component("CatalogVariantRef")["properties"]["display_price"]
        self.assertEqual(rendered.get("type"), "string", rendered)

    def test_every_catalog_route_is_present(self):
        """Cheap guard against a router registration being dropped."""
        paths = set(self.schema["paths"])
        for expected in (
            "/api/v1/core/catalog-categories/",
            "/api/v1/core/catalog-categories/tree/",
            "/api/v1/core/catalog-items/",
            "/api/v1/core/catalog-items/lookup/",
            "/api/v1/core/catalog-items/tags/",
            "/api/v1/core/catalog-variants/",
            "/api/v1/core/catalog-item-media/request-upload-url/",
            "/api/v1/core/catalog-item-media/confirm-upload/",
        ):
            with self.subTest(path=expected):
                self.assertIn(expected, paths)
