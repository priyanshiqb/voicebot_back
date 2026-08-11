"""
Item serializers.

Three things here carry most of the risk:

* **Nested variant creation.** `create()` writes the item, then its variants, then
  calls `ensure_default_variant()`. Get the order wrong and an item supplied with
  three prices gets a fourth junk `Standard` row alongside them.
* **Add-on validation up front.** The `m2m_changed` receiver is the backstop, but it
  raises inside Django's `atomic(savepoint=False)` — which marks the whole request
  transaction for rollback and surfaces as a 500. So the serializer has to reject a
  bad add-on *before* touching the relation, and these tests are what prove it does.
* **The array and JSON caps.** `attributes`, `tags` and `aliases` are unmetered writes
  a tenant can perform on their own rows, read back on every catalog response.
"""

from datetime import date

from core.models.catalog_item import CatalogItem
from core.models.catalog_variant import CatalogVariant
from core.models.event_template import EventTemplate
from core.serializers.catalog_item import (
    CatalogItemDropdownSerializer,
    CatalogItemListSerializer,
    CatalogItemLookupSerializer,
    CatalogItemRefSerializer,
    CatalogItemSerializer,
)
from core.tests.catalog.serializers.base import CatalogSerializerTestCase

Kind = CatalogItem.ItemKind
Mode = CatalogItem.FulfilmentMode
PricingType = CatalogVariant.PricingType


class ItemSerializerTestCase(CatalogSerializerTestCase):
    def payload(self, **overrides):
        data = {"item_kind": Kind.PHYSICAL_GOOD, "name": "Filter Coffee"}
        data.update(overrides)
        return data

    def build_item(self, **overrides):
        return self.build(CatalogItemSerializer, self.payload(**overrides))


class RequiredFieldTests(ItemSerializerTestCase):
    def test_item_kind_is_required(self):
        """No default, deliberately — it is what stops a catalog filing everything
        under whichever value happened to be chosen for us."""
        serializer = self.build(CatalogItemSerializer, {"name": "Coffee"})
        self.assert_field_error(serializer, "item_kind")

    def test_an_unknown_item_kind_is_rejected(self):
        serializer = self.build_item(item_kind="WIDGET")
        self.assert_field_error(serializer, "item_kind")

    def test_a_name_is_stripped_and_bounded(self):
        self.assertEqual(
            self.save_valid(self.build_item(name="  Coffee  ")).name, "Coffee"
        )
        self.assert_field_error(self.build_item(name="C"), "name", "at least 2")
        self.assert_field_error(self.build_item(name="x" * 201), "name")

    def test_a_minimal_create_needs_only_kind_and_name(self):
        item = self.save_valid(self.build_item())

        self.assertIsNone(item.category)
        self.assertEqual(item.tags, [])
        self.assertEqual(item.aliases, [])
        self.assertEqual(item.fulfilment_modes, [])
        self.assertEqual(item.attributes, {})
        self.assertEqual(item.item_code, "")
        self.assertEqual(item.tenant_id, self.tenant.id)


class ItemCodeTests(ItemSerializerTestCase):
    def test_a_duplicate_item_code_is_a_field_error(self):
        self.make_item("First", item_code="PRD-1")
        serializer = self.build_item(name="Second", item_code="PRD-1")
        self.assert_field_error(serializer, "item_code", "already uses the code")

    def test_item_codes_collide_case_insensitively(self):
        self.make_item("First", item_code="PRD-1")
        serializer = self.build_item(name="Second", item_code="prd-1")
        self.assert_field_error(serializer, "item_code")

    def test_an_item_code_is_stripped(self):
        item = self.save_valid(self.build_item(item_code="  PRD-1  "))
        self.assertEqual(item.item_code, "PRD-1")

    def test_many_items_may_have_no_item_code(self):
        self.make_item("First")
        serializer = self.build_item(name="Second")
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))

    def test_the_same_item_code_in_another_tenant_is_allowed(self):
        self.make_item("Theirs", tenant=self.other_tenant, item_code="PRD-1")
        serializer = self.build_item(item_code="PRD-1")
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))

    def test_keeping_its_own_code_on_update_is_allowed(self):
        item = self.make_item("First", item_code="PRD-1")
        serializer = self.build(
            CatalogItemSerializer,
            {"item_code": "PRD-1", "brand": "Acme"},
            instance=item,
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))

    def test_two_items_may_share_a_name(self):
        """A deliberate departure from every other named model in this app. A catalog
        is a data set, and two brands of "Basmati Rice 5 kg" is a correct pair."""
        self.make_item("Basmati Rice 5 kg")
        serializer = self.build_item(name="Basmati Rice 5 kg")
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))


class ArrayAndJsonTests(ItemSerializerTestCase):
    def test_tags_are_cleaned(self):
        item = self.save_valid(
            self.build_item(tags=["  vip ", "", "VIP", "organic"])
        )
        self.assertEqual(item.tags, ["vip", "organic"])

    def test_aliases_are_cleaned(self):
        item = self.save_valid(self.build_item(aliases=["Coke", "coke", " cola "]))
        self.assertEqual(item.aliases, ["Coke", "cola"])

    def test_too_many_tags_is_rejected(self):
        serializer = self.build_item(tags=[f"tag-{index}" for index in range(26)])
        self.assert_field_error(serializer, "tags", "At most 25")

    def test_too_many_aliases_is_rejected(self):
        serializer = self.build_item(
            aliases=[f"alias-{index}" for index in range(26)]
        )
        self.assert_field_error(serializer, "aliases", "At most 25")

    def test_an_over_long_tag_is_rejected(self):
        serializer = self.build_item(tags=["x" * 51])
        self.assert_field_error(serializer, "tags")

    def test_fulfilment_modes_are_de_duplicated_in_order(self):
        """Order-preserving: the first mode listed is the one a UI shows first, and a
        tenant who put DELIVERY first meant that."""
        item = self.save_valid(
            self.build_item(
                fulfilment_modes=[Mode.DELIVERY, Mode.DINE_IN, Mode.DELIVERY]
            )
        )
        self.assertEqual(item.fulfilment_modes, [Mode.DELIVERY, Mode.DINE_IN])

    def test_an_unknown_fulfilment_mode_is_rejected(self):
        serializer = self.build_item(fulfilment_modes=["TELEPORT"])
        self.assert_field_error(serializer, "fulfilment_modes")

    def test_attributes_must_be_an_object(self):
        serializer = self.build_item(attributes=["veg"])
        self.assert_field_error(serializer, "attributes", "JSON object")

    def test_attributes_are_bounded(self):
        serializer = self.build_item(attributes={"notes": "x" * 9000})
        self.assert_field_error(serializer, "attributes", "bytes")

    def test_nested_attributes_round_trip(self):
        payload = {"bhk": 3, "amenities": ["lift", "parking"], "furnishing": "semi"}
        item = self.save_valid(self.build_item(attributes=payload))
        item.refresh_from_db()
        self.assertEqual(item.attributes, payload)


class RelationTests(ItemSerializerTestCase):
    def _template(self, tenant=None):
        return EventTemplate.objects.create(
            tenant=tenant or self.tenant, name="Cleaning", duration_minutes=30
        )

    def test_a_cross_tenant_category_is_rejected(self):
        foreign = self.make_category("Menu", tenant=self.other_tenant)
        serializer = self.build_item(category=str(foreign.pk))
        self.assert_field_error(serializer, "category", "different tenant")

    def test_a_cross_tenant_event_template_is_rejected(self):
        serializer = self.build_item(
            item_kind=Kind.SERVICE,
            event_template=str(self._template(tenant=self.other_tenant).pk),
        )
        self.assert_field_error(serializer, "event_template", "different tenant")

    def test_only_a_service_can_be_bookable(self):
        serializer = self.build_item(
            item_kind=Kind.PHYSICAL_GOOD, event_template=str(self._template().pk)
        )
        self.assert_field_error(serializer, "event_template", "Only a service")

    def test_a_bookable_service_saves(self):
        template = self._template()
        item = self.save_valid(
            self.build_item(
                item_kind=Kind.SERVICE,
                name="Dental Cleaning",
                event_template=str(template.pk),
            )
        )
        self.assertEqual(item.event_template_id, template.pk)

    def test_a_category_is_optional(self):
        serializer = self.build_item()
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))

    def test_the_category_nests_on_read(self):
        category = self.make_category("Menu")
        item = self.save_valid(self.build_item(category=str(category.pk)))
        data = CatalogItemSerializer(item, context=self.context()).data
        self.assertEqual(data["category"]["name"], "Menu")


class AvailabilityWindowTests(ItemSerializerTestCase):
    def test_a_reversed_window_is_rejected(self):
        serializer = self.build_item(
            available_from="2026-06-30", available_until="2026-04-01"
        )
        self.assert_field_error(serializer, "available_until", "must not end before")

    def test_an_ordered_window_saves(self):
        item = self.save_valid(
            self.build_item(available_from="2026-04-01", available_until="2026-06-30")
        )
        self.assertEqual(item.available_from, date(2026, 4, 1))

    def test_one_open_end_is_allowed(self):
        self.assertTrue(
            self.build_item(available_from="2026-04-01").is_valid()
        )
        self.assertTrue(
            self.build_item(available_until="2026-06-30").is_valid()
        )


class AddOnTests(ItemSerializerTestCase):
    def setUp(self):
        super().setUp()
        self.cheese = self.make_item("Extra Cheese", item_kind=Kind.ADD_ON)

    def test_an_add_on_attaches(self):
        item = self.save_valid(self.build_item(add_on_items=[str(self.cheese.pk)]))
        self.assertEqual(list(item.add_on_items.all()), [self.cheese])

    def test_a_non_add_on_is_rejected_with_a_field_error(self):
        """Not a 500. The `m2m_changed` backstop raises inside
        `atomic(savepoint=False)`, which rolls the whole request back — so the
        serializer must catch this first."""
        fries = self.make_item("Fries", item_kind=Kind.PHYSICAL_GOOD)
        serializer = self.build_item(add_on_items=[str(fries.pk)])
        self.assert_field_error(serializer, "add_on_items", "Rejected: Fries")

    def test_the_message_explains_the_distinction(self):
        fries = self.make_item("Fries", item_kind=Kind.PHYSICAL_GOOD)
        serializer = self.build_item(add_on_items=[str(fries.pk)])
        errors = self.assert_field_error(serializer, "add_on_items")
        self.assertIn("picked 0..n times", str(errors["add_on_items"]))

    def test_a_cross_tenant_add_on_is_rejected(self):
        foreign = self.make_item(
            "Extra Cheese", tenant=self.other_tenant, item_kind=Kind.ADD_ON
        )
        serializer = self.build_item(add_on_items=[str(foreign.pk)])
        self.assert_field_error(serializer, "add_on_items", "same tenant")

    def test_an_item_cannot_be_its_own_add_on(self):
        item = self.make_item("Sauce", item_kind=Kind.ADD_ON)
        serializer = self.build(
            CatalogItemSerializer,
            {"add_on_items": [str(item.pk)]},
            instance=item,
            partial=True,
        )
        self.assert_field_error(serializer, "add_on_items", "its own add-on")

    def test_the_add_on_cap_is_enforced(self):
        with self.settings(CATALOG_MAX_ADD_ONS_PER_ITEM=1):
            second = self.make_item("Extra Sauce", item_kind=Kind.ADD_ON)
            serializer = self.build_item(
                add_on_items=[str(self.cheese.pk), str(second.pk)]
            )
            self.assert_field_error(serializer, "add_on_items", "At most 1")

    def test_add_ons_nest_on_read(self):
        item = self.save_valid(self.build_item(add_on_items=[str(self.cheese.pk)]))
        data = CatalogItemSerializer(item, context=self.context()).data
        self.assertEqual(data["add_on_items"][0]["name"], "Extra Cheese")
        self.assertEqual(data["add_on_items"][0]["item_kind"], "ADD_ON")


class NestedVariantTests(ItemSerializerTestCase):
    def test_variants_are_created_inline(self):
        item = self.save_valid(
            self.build_item(
                name="Pizza",
                variants=[
                    {"variant_label": "Small", "price": "200.00", "is_default": True},
                    {"variant_label": "Large", "price": "400.00", "sort_order": 1},
                ],
            )
        )
        self.assertEqual(item.variants.count(), 2)
        self.assertEqual(item.default_variant.variant_label, "Small")
        self.assertEqual(item.default_variant.display_price, "₹200")

    def test_nested_variants_inherit_the_tenant_and_audit_fields(self):
        """They are created inside `create()`, not through `perform_create`, so the
        viewset's audit injection never sees them — the serializer has to copy."""
        item = self.save_valid(
            self.build_item(variants=[{"variant_label": "Standard", "price": "10.00"}])
        )
        variant = item.variants.first()
        self.assertEqual(variant.tenant_id, self.tenant.id)
        self.assertEqual(variant.created_by, self.user)
        self.assertEqual(variant.updated_by, self.user)

    def test_an_item_with_no_variants_gets_a_standard_on_request_one(self):
        """ON_REQUEST, not a zero price — "we have not priced this yet" is the truth,
        and FREE or FIXED 0.00 would have the agent quote it as free."""
        item = self.save_valid(self.build_item(name="Consulting"))

        self.assertEqual(item.variants.count(), 1)
        default = item.default_variant
        self.assertEqual(default.variant_label, "Standard")
        self.assertEqual(default.pricing_type, PricingType.ON_REQUEST)
        self.assertIsNone(default.price)

    def test_supplied_variants_are_not_joined_by_a_junk_standard_row(self):
        """The ordering bug `ensure_default_variant()` exists to avoid: run it before
        the supplied variants are written and the item ends up with an extra row that
        nobody asked for, fighting over `is_default`."""
        item = self.save_valid(
            self.build_item(
                variants=[
                    {"variant_label": "Small", "price": "200.00"},
                    {"variant_label": "Large", "price": "400.00"},
                ]
            )
        )
        self.assertEqual(item.variants.count(), 2)
        self.assertNotIn("Standard", [v.variant_label for v in item.variants.all()])

    def test_the_first_variant_is_promoted_when_none_is_flagged_default(self):
        item = self.save_valid(
            self.build_item(
                variants=[
                    {"variant_label": "Small", "price": "200.00", "sort_order": 0},
                    {"variant_label": "Large", "price": "400.00", "sort_order": 1},
                ]
            )
        )
        self.assertEqual(item.default_variant.variant_label, "Small")

    def test_duplicate_variant_labels_are_rejected(self):
        serializer = self.build_item(
            variants=[
                {"variant_label": "Small", "price": "200.00"},
                {"variant_label": "Small", "price": "400.00"},
            ]
        )
        self.assert_field_error(serializer, "variants", "Duplicate variant labels")

    def test_two_default_variants_are_rejected(self):
        serializer = self.build_item(
            variants=[
                {"variant_label": "Small", "price": "200.00", "is_default": True},
                {"variant_label": "Large", "price": "400.00", "is_default": True},
            ]
        )
        self.assert_field_error(serializer, "variants", "Only one variant")

    def test_the_nested_variant_cap_is_enforced(self):
        with self.settings(CATALOG_MAX_VARIANTS_PER_ITEM=2):
            serializer = self.build_item(
                variants=[
                    {"variant_label": f"V{index}", "price": "10.00"}
                    for index in range(3)
                ]
            )
            self.assert_field_error(serializer, "variants", "At most 2")

    def test_nested_variants_run_the_same_pricing_rules(self):
        """The whole point of sharing `_VariantValidationMixin`: a price that would be
        rejected on `/catalog-variants/` must not sneak in through item create."""
        serializer = self.build_item(
            variants=[
                {
                    "variant_label": "Bad",
                    "pricing_type": PricingType.RECURRING,
                    "price": "100.00",
                }
            ]
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("variants", serializer.errors)

    def test_variants_are_rejected_on_update(self):
        item = self.make_item("Pizza")
        serializer = self.build(
            CatalogItemSerializer,
            {"variants": [{"variant_label": "Small", "price": "200.00"}]},
            instance=item,
            partial=True,
        )
        self.assert_field_error(serializer, "variants", "/api/v1/core/catalog-variants/")

    def test_an_update_that_omits_variants_is_unaffected(self):
        item = self.save_valid(
            self.build_item(variants=[{"variant_label": "Small", "price": "200.00"}])
        )
        serializer = self.build(
            CatalogItemSerializer, {"brand": "Acme"}, instance=item, partial=True
        )
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))
        updated = serializer.save(updated_by=self.user)
        self.assertEqual(updated.brand, "Acme")
        self.assertEqual(updated.variants.count(), 1)


class ReadOnlyFieldTests(ItemSerializerTestCase):
    def test_derived_fields_are_not_writable(self):
        serializer = self.build_item(
            item_kind_label="Nonsense", default_variant={"id": "x"}, media=[]
        )
        item = self.save_valid(serializer)
        data = CatalogItemSerializer(item, context=self.context()).data
        self.assertEqual(data["item_kind_label"], "Physical good")

    def test_tenant_is_not_writable(self):
        item = self.save_valid(self.build_item(tenant=str(self.other_tenant.pk)))
        self.assertEqual(item.tenant_id, self.tenant.id)

    def test_the_representation_carries_the_default_variant(self):
        item = self.save_valid(
            self.build_item(variants=[{"variant_label": "Small", "price": "200.00"}])
        )
        data = CatalogItemSerializer(item, context=self.context()).data
        self.assertEqual(data["default_variant"]["display_price"], "₹200")
        self.assertEqual(data["media"], [])


class RepresentationTests(ItemSerializerTestCase):
    def setUp(self):
        super().setUp()
        self.category = self.make_category("Starters")
        self.item = self.make_item(
            "Paneer Butter Masala",
            category=self.category,
            item_code="PBM-1",
            brand="House",
            short_description="Creamy tomato gravy.",
            aliases=["PBM"],
            tags=["bestseller"],
            attributes={"veg": True},
            fulfilment_modes=[Mode.DINE_IN],
            lead_time_minutes=20,
        )
        self.variant = self.make_variant(
            item=self.item, variant_label="Half", is_default=True
        )

    def test_the_ref_serializer_is_minimal(self):
        data = CatalogItemRefSerializer(self.item).data
        self.assertEqual(set(data), {"id", "name", "item_kind", "item_code"})

    def test_the_list_serializer_carries_a_rendered_price(self):
        """Not an amount and a currency code: a list showing "8.4% per annum" beside
        "₹50 base + ₹18 per kilometre" cannot be assembled client-side."""
        data = CatalogItemListSerializer(self.item).data
        self.assertEqual(data["price"], "₹100")
        self.assertEqual(data["variant_count"], 1)
        self.assertEqual(data["category_name"], "Starters")
        self.assertEqual(data["item_kind_label"], "Physical good")
        self.assertIsNone(data["primary_image_url"])
        for absent in ("created_by", "updated_by", "deleted_by", "tenant"):
            self.assertNotIn(absent, data)

    def test_the_list_price_is_null_when_there_is_no_default(self):
        bare = self.make_item("Unpriced")
        self.assertIsNone(CatalogItemListSerializer(bare).data["price"])

    def test_the_dropdown_label_includes_the_item_code(self):
        data = CatalogItemDropdownSerializer(self.item).data
        self.assertEqual(set(data), {"label", "value"})
        self.assertEqual(data["label"], "Paneer Butter Masala (PBM-1)")

    def test_the_dropdown_label_omits_an_empty_item_code(self):
        bare = self.make_item("Plain")
        self.assertEqual(CatalogItemDropdownSerializer(bare).data["label"], "Plain")

    def test_the_lookup_serializer_is_shaped_for_the_agent(self):
        """Everything a bot would not say out loud is left out — audit fields, sort
        order, timestamps."""
        data = CatalogItemLookupSerializer(self.item, context=self.context()).data

        self.assertEqual(
            set(data),
            {
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
            },
        )
        self.assertEqual(data["price"], "₹100")
        self.assertTrue(data["is_sellable"])
        self.assertEqual(data["variants"][0]["variant_label"], "Half")

    def test_lookup_reports_unsellable_when_every_variant_is(self):
        self.variant.availability_status = CatalogVariant.AvailabilityStatus.OUT_OF_STOCK
        self.variant.save(update_fields=["availability_status"])

        data = CatalogItemLookupSerializer(self.item, context=self.context()).data
        self.assertFalse(data["is_sellable"])

    def test_lookup_reports_sellable_when_any_variant_is(self):
        self.variant.availability_status = CatalogVariant.AvailabilityStatus.OUT_OF_STOCK
        self.variant.save(update_fields=["availability_status"])
        self.make_variant(item=self.item, variant_label="Full", sort_order=1)

        data = CatalogItemLookupSerializer(self.item, context=self.context()).data
        self.assertTrue(data["is_sellable"])
