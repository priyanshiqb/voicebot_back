"""
Item API.

`lookup` gets the most attention here — it is what the voice agent calls
mid-conversation, so its ranking, its result cap and the liveness of its prices are
product behaviour rather than implementation detail.

`CatalogItemFilter` gets the second most. Every filter it declares is documented, and
a documented filter that silently does nothing is worse than no filter at all: the
client believes it narrowed the list and shows the user the wrong thing. The
category API had exactly that bug, so every key here is asserted to actually filter.
"""

from django.db import connection
from django.test.utils import CaptureQueriesContext

from core.models.catalog_item import CatalogItem
from core.models.catalog_item_media import CatalogItemMedia
from core.models.catalog_variant import CatalogVariant
from core.tests.catalog.views.base import ITEMS, CatalogAPITestCase

Kind = CatalogItem.ItemKind
Mode = CatalogItem.FulfilmentMode
Status = CatalogVariant.AvailabilityStatus


class AuthTests(CatalogAPITestCase):
    def test_anonymous_is_refused(self):
        self.assertEqual(self.api_anon.get(ITEMS).status_code, 401)

    def test_a_machine_token_is_refused_everywhere(self):
        item = self.make_item()
        self.assertEqual(self.api_machine.get(ITEMS).status_code, 403)
        self.assertEqual(self.api_machine.get(f"{ITEMS}{item.pk}/").status_code, 403)
        self.assertEqual(
            self.api_machine.get(f"{ITEMS}lookup/", {"q": "coffee"}).status_code, 403
        )


class CreateTests(CatalogAPITestCase):
    def test_a_minimal_create_gets_an_auto_default_variant(self):
        """Every item always has a price to quote from, so no consumer has to branch
        on "does this item have variants?"."""
        data = self.assert_ok(
            self.api.post(
                ITEMS, {"item_kind": Kind.SERVICE, "name": "Consulting"}, format="json"
            ),
            201,
        )
        self.assertEqual(data["default_variant"]["variant_label"], "Standard")
        self.assertEqual(data["default_variant"]["display_price"], "Price on request")

    def test_variants_may_be_supplied_inline(self):
        data = self.assert_ok(
            self.api.post(
                ITEMS,
                {
                    "item_kind": Kind.PHYSICAL_GOOD,
                    "name": "Pizza",
                    "variants": [
                        {"variant_label": "Small", "price": "200.00", "is_default": True},
                        {"variant_label": "Large", "price": "400.00", "sort_order": 1},
                    ],
                },
                format="json",
            ),
            201,
        )
        self.assertEqual(len(data["variants"]), 2)
        self.assertEqual(data["default_variant"]["display_price"], "₹200")

    def test_a_bad_nested_price_fails_the_whole_create(self):
        """`create()` is atomic — an item written without the prices the request asked
        for is worse than no item."""
        response = self.api.post(
            ITEMS,
            {
                "item_kind": Kind.PHYSICAL_GOOD,
                "name": "Pizza",
                "variants": [
                    {
                        "variant_label": "Bad",
                        "pricing_type": "RECURRING",
                        "price": "100.00",
                    }
                ],
            },
            format="json",
        )
        self.assert_error(response, 400, field="variants")
        self.assertFalse(CatalogItem.objects.filter(name="Pizza").exists())

    def test_variants_are_rejected_on_update(self):
        item = self.make_item("Pizza")
        self.assert_error(
            self.api.patch(
                f"{ITEMS}{item.pk}/",
                {"variants": [{"variant_label": "X", "price": "1.00"}]},
                format="json",
            ),
            400,
            field="variants",
            contains="/api/v1/core/catalog-variants/",
        )

    def test_a_duplicate_item_code_is_a_400_on_the_field(self):
        self.make_item("First", item_code="PRD-1")
        self.assert_error(
            self.api.post(
                ITEMS,
                {"item_kind": Kind.PHYSICAL_GOOD, "name": "Second", "item_code": "prd-1"},
                format="json",
            ),
            field="item_code",
        )

    def test_add_ons_attach_and_a_wrong_kind_is_a_400(self):
        """Not a 500. The m2m_changed backstop raises inside
        `atomic(savepoint=False)`, which would roll the request back as a server
        error — the serializer has to catch it first."""
        item = self.make_item("Burger")
        cheese = self.make_item("Extra Cheese", item_kind=Kind.ADD_ON)
        fries = self.make_item("Fries", item_kind=Kind.PHYSICAL_GOOD)

        data = self.assert_ok(
            self.api.patch(
                f"{ITEMS}{item.pk}/", {"add_on_items": [str(cheese.pk)]}, format="json"
            )
        )
        self.assertEqual(data["add_on_items"][0]["name"], "Extra Cheese")

        self.assert_error(
            self.api.patch(
                f"{ITEMS}{item.pk}/", {"add_on_items": [str(fries.pk)]}, format="json"
            ),
            400,
            field="add_on_items",
            contains="Rejected: Fries",
        )


class DeleteTests(CatalogAPITestCase):
    def test_delete_cascades_to_variants_and_media(self):
        item = self.make_item("Pizza")
        variant = self.make_variant(item=item)
        media = CatalogItemMedia.objects.create(
            tenant=self.tenant, item=item, original_name="p.jpg"
        )

        self.assertEqual(self.api.delete(f"{ITEMS}{item.pk}/").status_code, 200)

        self.assertTrue(CatalogVariant.all_objects.get(pk=variant.pk).is_deleted)
        self.assertTrue(CatalogItemMedia.all_objects.get(pk=media.pk).is_deleted)

    def test_bulk_delete(self):
        first = self.make_item("A")
        second = self.make_item("B")
        self.assert_ok(
            self.api.post(
                f"{ITEMS}bulk_delete/",
                {"ids": [str(first.pk), str(second.pk)]},
                format="json",
            )
        )
        self.assertEqual(CatalogItem.objects.count(), 0)

    def test_bulk_delete_refuses_a_foreign_id(self):
        mine = self.make_item("Mine")
        theirs = self.make_item("Theirs", tenant=self.other_tenant)
        self.assert_error(
            self.api.post(
                f"{ITEMS}bulk_delete/",
                {"ids": [str(mine.pk), str(theirs.pk)]},
                format="json",
            ),
            400,
        )
        mine.refresh_from_db()
        self.assertFalse(mine.is_deleted)


class ListingTests(CatalogAPITestCase):
    def setUp(self):
        super().setUp()
        self.category = self.make_category("Starters")
        self.pizza = self.make_item(
            "Paneer Pizza",
            category=self.category,
            brand="House",
            tags=["bestseller", "veg"],
            aliases=["PBM"],
            item_code="PZ-1",
            is_featured=True,
        )
        self.make_variant(item=self.pizza, price="200.00", is_default=True)

        self.service = self.make_item(
            "Consulting", item_kind=Kind.SERVICE, tags=["veg"], brand="Other"
        )
        self.make_variant(item=self.service, price="5000.00", is_default=True)

    def test_the_list_row_carries_a_rendered_price(self):
        rows = self.assert_paginated(self.api.get(ITEMS), count=2)
        pizza = next(r for r in rows if r["name"] == "Paneer Pizza")
        self.assertEqual(pizza["price"], "₹200")
        self.assertEqual(pizza["variant_count"], 1)
        self.assertEqual(pizza["category_name"], "Starters")
        self.assertIsNone(pizza["primary_image_url"])

    def test_filter_by_item_kind(self):
        rows = self.assert_paginated(
            self.api.get(ITEMS, {"item_kind": Kind.SERVICE}), count=1
        )
        self.assertEqual(rows[0]["name"], "Consulting")

    def test_filter_by_category(self):
        self.assert_paginated(
            self.api.get(ITEMS, {"category": str(self.category.pk)}), count=1
        )

    def test_filter_by_brand(self):
        self.assert_paginated(self.api.get(ITEMS, {"brand": "House"}), count=1)

    def test_filter_by_is_featured(self):
        self.assert_paginated(self.api.get(ITEMS, {"is_featured": "true"}), count=1)

    def test_filter_by_tags_is_any_of(self):
        rows = self.assert_paginated(
            self.api.get(ITEMS, {"tags": "bestseller,missing"}), count=1
        )
        self.assertEqual(rows[0]["name"], "Paneer Pizza")

    def test_filter_by_tags_all_requires_every_tag(self):
        self.assert_paginated(self.api.get(ITEMS, {"tags_all": "veg"}), count=2)
        self.assert_paginated(
            self.api.get(ITEMS, {"tags_all": "veg,bestseller"}), count=1
        )

    def test_filter_by_alias(self):
        rows = self.assert_paginated(self.api.get(ITEMS, {"alias": "PBM"}), count=1)
        self.assertEqual(rows[0]["name"], "Paneer Pizza")

    def test_filter_by_price_range(self):
        self.assert_paginated(self.api.get(ITEMS, {"price_min": "1000"}), count=1)
        self.assert_paginated(self.api.get(ITEMS, {"price_max": "1000"}), count=1)

    def test_filter_by_has_media(self):
        CatalogItemMedia.objects.create(
            tenant=self.tenant,
            item=self.pizza,
            original_name="p.jpg",
            upload_status=CatalogItemMedia.UploadStatus.CONFIRMED,
        )
        rows = self.assert_paginated(self.api.get(ITEMS, {"has_media": "true"}), count=1)
        self.assertEqual(rows[0]["name"], "Paneer Pizza")

        self.assert_paginated(self.api.get(ITEMS, {"has_media": "false"}), count=1)

    def test_an_unconfirmed_upload_does_not_count_as_media(self):
        """A PENDING row points at a key that may have nothing behind it."""
        CatalogItemMedia.objects.create(
            tenant=self.tenant,
            item=self.pizza,
            original_name="p.jpg",
            upload_status=CatalogItemMedia.UploadStatus.PENDING,
        )
        self.assert_paginated(self.api.get(ITEMS, {"has_media": "true"}), count=0)

    def test_search_matches_name_code_and_brand(self):
        self.assert_paginated(self.api.get(ITEMS, {"search": "Paneer"}), count=1)
        self.assert_paginated(self.api.get(ITEMS, {"search": "PZ-1"}), count=1)
        self.assert_paginated(self.api.get(ITEMS, {"search": "House"}), count=1)

    def test_ordering_by_name(self):
        rows = self.assert_paginated(self.api.get(ITEMS, {"ordering": "name"}))
        self.assertEqual([r["name"] for r in rows], ["Consulting", "Paneer Pizza"])

    def test_the_list_query_count_does_not_grow_with_rows(self):
        with CaptureQueriesContext(connection) as baseline:
            self.api.get(ITEMS)

        for index in range(5):
            item = self.make_item(f"Extra {index}")
            self.make_variant(item=item, is_default=True)

        with CaptureQueriesContext(connection) as grown:
            self.api.get(ITEMS)

        self.assertEqual(
            len(grown.captured_queries), len(baseline.captured_queries)
        )


class LookupTests(CatalogAPITestCase):
    def setUp(self):
        super().setUp()
        self.coke = self.make_item(
            "Coca-Cola 500 ml",
            brand="Coca-Cola",
            aliases=["coke", "cola"],
            short_description="Chilled soft drink.",
        )
        self.variant = self.make_variant(
            item=self.coke, price="45.00", sku="CC-500", is_default=True
        )

    def test_an_alias_resolves_to_the_item(self):
        """ASR returns the caller's words, not the catalog's."""
        data = self.assert_ok(self.api.get(f"{ITEMS}lookup/", {"q": "coke"}))
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "Coca-Cola 500 ml")

    def test_the_name_resolves_too(self):
        data = self.assert_ok(self.api.get(f"{ITEMS}lookup/", {"q": "Coca-Cola"}))
        self.assertEqual(data[0]["name"], "Coca-Cola 500 ml")

    def test_a_sku_resolves(self):
        data = self.assert_ok(self.api.get(f"{ITEMS}lookup/", {"q": "CC-500"}))
        self.assertEqual(data[0]["name"], "Coca-Cola 500 ml")

    def test_the_payload_is_shaped_for_the_agent(self):
        data = self.assert_ok(self.api.get(f"{ITEMS}lookup/", {"q": "coke"}))
        row = data[0]
        self.assertEqual(row["price"], "₹45")
        self.assertTrue(row["is_sellable"])
        self.assertEqual(row["aliases"], ["coke", "cola"])
        for absent in ("created_by", "sort_order", "created_at", "tenant"):
            self.assertNotIn(absent, row)

    def test_the_price_is_read_live(self):
        """The reason this is a structured lookup and not RAG content: a repriced
        variant is reflected on the very next call, with no re-indexing."""
        self.variant.price = "55.00"
        self.variant.save(update_fields=["price"])

        data = self.assert_ok(self.api.get(f"{ITEMS}lookup/", {"q": "coke"}))
        self.assertEqual(data[0]["price"], "₹55")

    def test_a_short_query_is_rejected(self):
        self.assert_error(
            self.api.get(f"{ITEMS}lookup/", {"q": "c"}),
            400,
            contains="at least 2 characters",
        )

    def test_a_missing_query_is_rejected(self):
        self.assert_error(self.api.get(f"{ITEMS}lookup/"), 400)

    def test_no_match_is_an_empty_list_not_an_error(self):
        data = self.assert_ok(self.api.get(f"{ITEMS}lookup/", {"q": "biryani"}))
        self.assertEqual(data, [])

    def test_inactive_items_are_never_returned(self):
        self.coke.is_active = False
        self.coke.save(update_fields=["is_active"])
        data = self.assert_ok(self.api.get(f"{ITEMS}lookup/", {"q": "coke"}))
        self.assertEqual(data, [])

    def test_sellable_only_drops_unsellable_items(self):
        self.variant.availability_status = Status.OUT_OF_STOCK
        self.variant.save(update_fields=["availability_status"])

        self.assertEqual(
            len(self.assert_ok(self.api.get(f"{ITEMS}lookup/", {"q": "coke"}))), 1
        )
        self.assertEqual(
            len(
                self.assert_ok(
                    self.api.get(
                        f"{ITEMS}lookup/", {"q": "coke", "sellable_only": "true"}
                    )
                )
            ),
            0,
        )

    def test_results_are_capped(self):
        """A bot listing fifteen options is a bot the caller hangs up on."""
        for index in range(15):
            self.make_item(f"Cola Variant {index}", aliases=["cola"])

        data = self.assert_ok(self.api.get(f"{ITEMS}lookup/", {"q": "cola"}))
        self.assertEqual(len(data), 10)

    def test_results_are_not_duplicated_across_match_passes(self):
        """The item matches on alias, name and brand. Ranked passes must de-duplicate
        or the agent reads the same product three times."""
        data = self.assert_ok(self.api.get(f"{ITEMS}lookup/", {"q": "cola"}))
        self.assertEqual(len({row["id"] for row in data}), len(data))

    def test_lookup_is_tenant_scoped(self):
        self.make_item("Their Coke", tenant=self.other_tenant, aliases=["coke"])
        data = self.assert_ok(self.api.get(f"{ITEMS}lookup/", {"q": "coke"}))
        self.assertEqual([row["name"] for row in data], ["Coca-Cola 500 ml"])


class TagFacetTests(CatalogAPITestCase):
    def test_tags_are_counted_and_sorted(self):
        self.make_item("A", tags=["bestseller", "veg"])
        self.make_item("B", tags=["veg"])
        self.make_item("C", tags=["veg"])

        data = self.assert_ok(self.api.get(f"{ITEMS}tags/"))
        self.assertEqual(data[0], {"tag": "veg", "count": 3})
        self.assertEqual(data[1], {"tag": "bestseller", "count": 1})

    def test_an_empty_catalog_returns_an_empty_facet_list(self):
        self.assertEqual(self.assert_ok(self.api.get(f"{ITEMS}tags/")), [])

    def test_the_facets_are_tenant_scoped(self):
        self.make_item("Theirs", tenant=self.other_tenant, tags=["secret"])
        data = self.assert_ok(self.api.get(f"{ITEMS}tags/"))
        self.assertNotIn("secret", [row["tag"] for row in data])


class MediaActionTests(CatalogAPITestCase):
    def test_only_confirmed_media_is_served(self):
        """PENDING rows point at keys that may have nothing behind them — serving them
        produces broken images with presigned URLs that 404."""
        item = self.make_item("Pizza")
        CatalogItemMedia.objects.create(
            tenant=self.tenant,
            item=item,
            original_name="ok.jpg",
            upload_status=CatalogItemMedia.UploadStatus.CONFIRMED,
        )
        CatalogItemMedia.objects.create(
            tenant=self.tenant,
            item=item,
            original_name="pending.jpg",
            upload_status=CatalogItemMedia.UploadStatus.PENDING,
        )

        data = self.assert_ok(self.api.get(f"{ITEMS}{item.pk}/media/"))
        self.assertEqual([row["original_name"] for row in data], ["ok.jpg"])

    def test_another_tenants_item_is_404(self):
        theirs = self.make_item("Theirs", tenant=self.other_tenant)
        self.assertEqual(self.api.get(f"{ITEMS}{theirs.pk}/media/").status_code, 404)


class DropdownTests(CatalogAPITestCase):
    def test_the_label_includes_the_item_code(self):
        self.make_item("Pizza", item_code="PZ-1")
        rows = self.assert_paginated(self.api.get(f"{ITEMS}dropdown/"))
        self.assertEqual(rows[0]["label"], "Pizza (PZ-1)")
        self.assertEqual(set(rows[0]), {"label", "value"})


class TenantIsolationTests(CatalogAPITestCase):
    def setUp(self):
        super().setUp()
        self.mine = self.make_item("Mine")
        self.theirs = self.make_item("Theirs", tenant=self.other_tenant)

    def test_the_list_shows_only_the_callers_tenant(self):
        rows = self.assert_paginated(self.api.get(ITEMS), count=1)
        self.assertEqual(rows[0]["name"], "Mine")

    def test_another_tenants_item_is_404_on_every_verb(self):
        for method, kwargs in (
            ("get", {}),
            ("patch", {"data": {"name": "Hijacked"}, "format": "json"}),
            ("delete", {}),
        ):
            with self.subTest(method=method):
                response = getattr(self.api, method)(
                    f"{ITEMS}{self.theirs.pk}/", **kwargs
                )
                self.assertEqual(response.status_code, 404, response.data)

    def test_a_cross_tenant_category_is_a_400_on_category(self):
        foreign_category = self.make_category("Theirs", tenant=self.other_tenant)
        self.assert_error(
            self.api.post(
                ITEMS,
                {
                    "item_kind": Kind.PHYSICAL_GOOD,
                    "name": "Sneaky",
                    "category": str(foreign_category.pk),
                },
                format="json",
            ),
            field="category",
            contains="different tenant",
        )

    def test_each_tenant_sees_its_own_row_for_the_same_name(self):
        self.make_item("Shared Name")
        self.make_item("Shared Name", tenant=self.other_tenant)

        mine = self.assert_paginated(self.api.get(ITEMS, {"search": "Shared"}), count=1)
        theirs = self.assert_paginated(
            self.api_other.get(ITEMS, {"search": "Shared"}), count=1
        )
        self.assertNotEqual(mine[0]["id"], theirs[0]["id"])
