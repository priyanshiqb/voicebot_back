"""
Variant serializers — the pricing surface.

`test_catalog_common` covers the pricing rules as a function; this file covers them as
an **API contract**: that each one surfaces on the right field, through a bound
serializer, with the request context that carries the tenant.

The distinction is not academic. A rule that fires but lands in `non_field_errors`
renders as a banner the user cannot act on, and a rule that fires only in
`validate_variant_pricing` but is never reached because the serializer forgot to call
it is a 500 in production and a green test here.
"""

from decimal import Decimal

from core.models.catalog_item import CatalogItem
from core.models.catalog_variant import CatalogVariant
from core.models.event_template import EventTemplate
from core.serializers.catalog_variant import (
    CatalogVariantDropdownSerializer,
    CatalogVariantListSerializer,
    CatalogVariantRefSerializer,
    CatalogVariantSerializer,
)
from core.tests.catalog.serializers.base import CatalogSerializerTestCase

PricingType = CatalogVariant.PricingType
Unit = CatalogVariant.UnitOfMeasure
Interval = CatalogVariant.RecurringInterval
Status = CatalogVariant.AvailabilityStatus


class VariantSerializerTestCase(CatalogSerializerTestCase):
    def setUp(self):
        super().setUp()
        self.item = self.make_item("Pizza")

    def payload(self, **overrides):
        data = {
            "item": str(self.item.pk),
            "variant_label": "Standard",
            "price": "100.00",
        }
        data.update(overrides)
        return data

    def build_variant(self, instance=None, partial=False, **overrides):
        return self.build(
            CatalogVariantSerializer,
            self.payload(**overrides) if instance is None else overrides,
            instance=instance,
            partial=partial,
        )


class LabelAndIdentityTests(VariantSerializerTestCase):
    def test_a_label_is_stripped(self):
        variant = self.save_valid(self.build_variant(variant_label="  Large  "))
        self.assertEqual(variant.variant_label, "Large")

    def test_a_whitespace_only_label_is_rejected(self):
        """DRF's CharField trims and then refuses the empty result, before
        `validate_variant_label` is reached. Asserted here so nobody adds a
        hand-written blank check that can never fire."""
        serializer = self.build_variant(variant_label="   ")
        self.assert_field_error(serializer, "variant_label", "may not be blank")

    def test_an_over_long_label_is_rejected(self):
        serializer = self.build_variant(variant_label="x" * 121)
        self.assert_field_error(serializer, "variant_label")

    def test_the_label_defaults_when_omitted(self):
        data = self.payload()
        data.pop("variant_label")
        serializer = self.build(CatalogVariantSerializer, data)
        variant = self.save_valid(serializer)
        self.assertEqual(variant.variant_label, "Standard")

    def test_sku_and_barcode_are_stripped(self):
        variant = self.save_valid(
            self.build_variant(sku="  ABC-1 ", barcode=" 890123 ")
        )
        self.assertEqual(variant.sku, "ABC-1")
        self.assertEqual(variant.barcode, "890123")

    def test_options_must_be_an_object(self):
        serializer = self.build_variant(options=["L", "Red"])
        self.assert_field_error(serializer, "options", "JSON object")

    def test_options_are_bounded(self):
        serializer = self.build_variant(options={"note": "x" * 9000})
        self.assert_field_error(serializer, "options", "bytes")

    def test_options_round_trip(self):
        variant = self.save_valid(
            self.build_variant(options={"size": "L", "colour": "Red"})
        )
        self.assertEqual(variant.options, {"size": "L", "colour": "Red"})


class SkuUniquenessTests(VariantSerializerTestCase):
    def test_a_duplicate_sku_is_a_field_error_not_a_500(self):
        self.make_variant(item=self.item, variant_label="A", sku="ABC-1")
        serializer = self.build_variant(variant_label="B", sku="ABC-1")
        self.assert_field_error(serializer, "sku", "already uses the SKU")

    def test_skus_collide_case_insensitively(self):
        """Matched with `Lower()` so the check agrees with the partial unique
        constraint; a raw comparison would pass "abc-1" and then fail in the database."""
        self.make_variant(item=self.item, variant_label="A", sku="ABC-1")
        serializer = self.build_variant(variant_label="B", sku="abc-1")
        self.assert_field_error(serializer, "sku")

    def test_many_variants_may_have_no_sku(self):
        self.make_variant(item=self.item, variant_label="A")
        serializer = self.build_variant(variant_label="B")
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))

    def test_the_same_sku_in_another_tenant_is_allowed(self):
        foreign_item = self.make_item("Pizza", tenant=self.other_tenant)
        self.make_variant(item=foreign_item, sku="ABC-1")

        serializer = self.build_variant(sku="ABC-1")
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))

    def test_keeping_its_own_sku_on_update_is_allowed(self):
        variant = self.make_variant(item=self.item, sku="ABC-1")
        serializer = self.build(
            CatalogVariantSerializer,
            {"sku": "ABC-1", "sort_order": 3},
            instance=variant,
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))

    def test_a_soft_deleted_sku_does_not_block_reuse(self):
        original = self.make_variant(item=self.item, variant_label="A", sku="ABC-1")
        original.soft_delete()

        serializer = self.build_variant(variant_label="B", sku="ABC-1")
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))


class PricingContractTests(VariantSerializerTestCase):
    """Each rule, through the serializer, on the field a form would highlight."""

    def test_fixed_without_a_price_errors_on_price(self):
        serializer = self.build_variant(price=None)
        self.assert_field_error(serializer, "price", "ON_REQUEST")

    def test_on_request_accepts_no_price(self):
        serializer = self.build_variant(
            pricing_type=PricingType.ON_REQUEST, price=None
        )
        variant = self.save_valid(serializer)
        self.assertIsNone(variant.price)
        self.assertFalse(variant.is_quotable)

    def test_free_accepts_no_price(self):
        variant = self.save_valid(
            self.build_variant(pricing_type=PricingType.FREE, price=None)
        )
        self.assertEqual(variant.display_price, "Free")

    def test_recurring_without_an_interval_errors_on_the_interval(self):
        serializer = self.build_variant(
            pricing_type=PricingType.RECURRING, price="12000.00"
        )
        self.assert_field_error(serializer, "recurring_interval", "needs an interval")

    def test_an_interval_on_a_fixed_row_errors_on_the_interval(self):
        serializer = self.build_variant(recurring_interval=Interval.MONTH)
        self.assert_field_error(
            serializer, "recurring_interval", "only applies to RECURRING"
        )

    def test_a_valid_recurring_price_saves(self):
        variant = self.save_valid(
            self.build_variant(
                pricing_type=PricingType.RECURRING,
                price="12000.00",
                recurring_interval=Interval.YEAR,
            )
        )
        self.assertEqual(variant.display_price, "₹12,000 per year")

    def test_range_without_a_max_errors_on_max_price(self):
        serializer = self.build_variant(pricing_type=PricingType.RANGE)
        self.assert_field_error(serializer, "max_price", "upper bound")

    def test_an_inverted_range_errors_on_max_price(self):
        serializer = self.build_variant(
            pricing_type=PricingType.RANGE, price="500.00", max_price="400.00"
        )
        self.assert_field_error(serializer, "max_price")

    def test_a_rate_over_100_errors_on_price(self):
        serializer = self.build_variant(
            pricing_type=PricingType.RATE, price="840.00", unit_of_measure=Unit.PERCENT
        )
        self.assert_field_error(serializer, "price", "percentage")

    def test_a_valid_rate_saves_and_renders(self):
        variant = self.save_valid(
            self.build_variant(
                pricing_type=PricingType.RATE,
                price="8.40",
                unit_of_measure=Unit.PERCENT,
                unit_label="per annum on the loan amount",
            )
        )
        self.assertEqual(
            variant.display_price, "8.4% per annum on the loan amount"
        )

    def test_a_composite_price_saves_and_renders(self):
        variant = self.save_valid(
            self.build_variant(
                base_price="50.00", price="18.00", unit_of_measure=Unit.KM
            )
        )
        self.assertEqual(
            variant.display_price, "₹50 base + ₹18 per kilometre"
        )

    def test_each_money_field_rejects_a_negative(self):
        for field in ("price", "base_price", "compare_at_price", "deposit_amount"):
            with self.subTest(field=field):
                serializer = self.build_variant(**{field: "-1.00"})
                self.assert_field_error(serializer, field, "negative")

    def test_a_tax_rate_over_100_is_rejected(self):
        serializer = self.build_variant(tax_rate_percentage="101.00")
        self.assert_field_error(serializer, "tax_rate_percentage")

    def test_stock_without_tracking_is_rejected(self):
        serializer = self.build_variant(track_inventory=False, stock_quantity=5)
        self.assert_field_error(serializer, "stock_quantity", "track_inventory")

    def test_tracked_stock_saves(self):
        variant = self.save_valid(
            self.build_variant(track_inventory=True, stock_quantity=4)
        )
        self.assertEqual(variant.stock_quantity, 4)
        self.assertTrue(variant.is_sellable)


class CurrencyTests(VariantSerializerTestCase):
    def test_a_lowercase_currency_is_upper_cased(self):
        variant = self.save_valid(self.build_variant(currency="usd"))
        self.assertEqual(variant.currency, "USD")

    def test_a_malformed_currency_is_rejected(self):
        for value in ("IN", "INRR", "IN1"):
            with self.subTest(value=value):
                serializer = self.build_variant(currency=value)
                self.assert_field_error(serializer, "currency", "ISO-4217")

    def test_the_currency_defaults_to_the_setting(self):
        data = self.payload()
        serializer = self.build(CatalogVariantSerializer, data)
        self.assertEqual(self.save_valid(serializer).currency, "INR")

    def test_an_unknown_but_well_formed_currency_falls_back_to_its_code(self):
        variant = self.save_valid(self.build_variant(currency="AED", price="500.00"))
        self.assertEqual(variant.display_price, "AED 500")


class CrossTenantAndModelRuleTests(VariantSerializerTestCase):
    def test_an_item_in_another_tenant_is_rejected(self):
        foreign_item = self.make_item("Pizza", tenant=self.other_tenant)
        serializer = self.build(
            CatalogVariantSerializer,
            {
                "item": str(foreign_item.pk),
                "variant_label": "Standard",
                "price": "100.00",
            },
        )
        self.assert_field_error(serializer, "item", "different tenant")

    def test_duration_is_rejected_when_the_item_is_booked_by_template(self):
        """`CatalogVariant.clean()` carries this rule, and DRF never calls
        `full_clean()` — so without the serializer invoking it, two durations that can
        disagree get written through the API and surface as a double-booked host."""
        template = EventTemplate.objects.create(
            tenant=self.tenant, name="Cleaning", duration_minutes=30
        )
        bookable = self.make_item(
            "Dental Cleaning",
            item_kind=CatalogItem.ItemKind.SERVICE,
            event_template=template,
        )
        serializer = self.build(
            CatalogVariantSerializer,
            {
                "item": str(bookable.pk),
                "variant_label": "Standard",
                "price": "1500.00",
                "duration_minutes": 45,
            },
        )
        self.assert_field_error(serializer, "duration_minutes", "event template")

    def test_duration_is_allowed_without_a_template(self):
        serializer = self.build_variant(duration_minutes=30)
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))

    def test_the_variant_cap_is_enforced_on_create(self):
        with self.settings(CATALOG_MAX_VARIANTS_PER_ITEM=2):
            self.make_variant(item=self.item, variant_label="A")
            self.make_variant(item=self.item, variant_label="B")

            serializer = self.build_variant(variant_label="C")
            self.assert_field_error(serializer, "item", "maximum is 2")

    def test_the_cap_does_not_block_editing_an_existing_variant(self):
        with self.settings(CATALOG_MAX_VARIANTS_PER_ITEM=1):
            variant = self.make_variant(item=self.item)
            serializer = self.build(
                CatalogVariantSerializer,
                {"price": "150.00"},
                instance=variant,
                partial=True,
            )
            self.assertTrue(serializer.is_valid(), dict(serializer.errors))


class PartialUpdateTests(VariantSerializerTestCase):
    def test_a_coherence_rule_reads_unchanged_fields_off_the_instance(self):
        """A PATCH sends a subset. `pricing_type=RANGE` alone, on a row with no
        `max_price`, must still be rejected."""
        variant = self.make_variant(item=self.item, price=Decimal("100.00"))
        serializer = self.build(
            CatalogVariantSerializer,
            {"pricing_type": PricingType.RANGE},
            instance=variant,
            partial=True,
        )
        self.assert_field_error(serializer, "max_price")

    def test_a_partial_update_supplying_the_missing_half_passes(self):
        variant = self.make_variant(item=self.item, price=Decimal("100.00"))
        serializer = self.build(
            CatalogVariantSerializer,
            {"pricing_type": PricingType.RANGE, "max_price": "500.00"},
            instance=variant,
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))

    def test_repricing_is_a_one_field_patch(self):
        variant = self.make_variant(item=self.item, price=Decimal("100.00"))
        serializer = self.build(
            CatalogVariantSerializer,
            {"price": "125.00"},
            instance=variant,
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))
        updated = serializer.save(updated_by=self.user)
        self.assertEqual(updated.price, Decimal("125.00"))
        self.assertEqual(updated.display_price, "₹125")


class ReadOnlyFieldTests(VariantSerializerTestCase):
    def test_the_derived_fields_are_not_writable(self):
        serializer = self.build_variant(
            display_price="₹1", is_sellable=False, unit_phrase="nonsense"
        )
        variant = self.save_valid(serializer)
        self.assertEqual(variant.display_price, "₹100")
        self.assertTrue(variant.is_sellable)

    def test_the_representation_carries_every_derived_field(self):
        variant = self.make_variant(item=self.item, unit_of_measure=Unit.HOUR)
        data = CatalogVariantSerializer(variant, context=self.context()).data

        self.assertEqual(data["display_price"], "₹100 per hour")
        self.assertEqual(data["unit_phrase"], "per hour")
        self.assertTrue(data["is_quotable"])
        self.assertTrue(data["is_sellable"])
        self.assertEqual(data["item_name"], "Pizza")
        self.assertEqual(data["item_kind"], "PHYSICAL_GOOD")
        self.assertEqual(data["pricing_type_label"], "Fixed price")
        self.assertEqual(data["availability_status_label"], "In stock")

    def test_is_sellable_folds_in_the_parent_item(self):
        """The single definition of "can we sell this right now?". If the serializer
        stopped exposing it, every consumer would reassemble it from five fields and
        one would get it wrong."""
        self.item.is_active = False
        self.item.save(update_fields=["is_active"])
        variant = self.make_variant(item=self.item)

        data = CatalogVariantSerializer(variant, context=self.context()).data
        self.assertFalse(data["is_sellable"])


class RepresentationTests(VariantSerializerTestCase):
    def setUp(self):
        super().setUp()
        self.variant = self.make_variant(
            item=self.item, variant_label="Large", sku="PZ-L", is_default=True
        )

    def test_the_ref_serializer_carries_the_rendered_price(self):
        """Not the raw amount: a caller rendering a reference wants the sentence, and
        composing it server-side is what stops voice and dashboard disagreeing."""
        data = CatalogVariantRefSerializer(self.variant).data
        self.assertEqual(
            set(data), {"id", "variant_label", "sku", "display_price", "is_default"}
        )
        self.assertEqual(data["display_price"], "₹100")

    def test_the_list_serializer_drops_the_audit_block_but_keeps_the_two_columns(self):
        data = CatalogVariantListSerializer(self.variant).data
        self.assertEqual(data["display_price"], "₹100")
        self.assertTrue(data["is_sellable"])
        self.assertEqual(data["item_name"], "Pizza")
        for absent in ("created_by", "updated_by", "deleted_by", "tenant"):
            self.assertNotIn(absent, data)

    def test_the_dropdown_label_carries_the_price(self):
        """What disambiguates two options with the same name across different items —
        and what a picker is being used to choose between."""
        data = CatalogVariantDropdownSerializer(self.variant).data
        self.assertEqual(set(data), {"label", "value"})
        self.assertEqual(data["label"], "Large — ₹100")

    def test_an_on_request_variant_reads_as_such_everywhere(self):
        variant = self.make_variant(
            item=self.item,
            variant_label="Bespoke",
            pricing_type=PricingType.ON_REQUEST,
            price=None,
        )
        self.assertEqual(
            CatalogVariantRefSerializer(variant).data["display_price"],
            "Price on request",
        )
        self.assertEqual(
            CatalogVariantDropdownSerializer(variant).data["label"],
            "Bespoke — Price on request",
        )
