"""
The catalog variant — where price and stock live.

Most of this file writes through `QuerySet.update()` rather than the model API. That
is deliberate: the point of putting the pricing rules in CHECK constraints was that
the serializer is *not* what holds the line. A test that only goes through
`full_clean()` would pass just as happily with no constraints at all, and the next
writer of this table will be a bulk importer or a data fix.
"""

from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from core.models.catalog_item import CatalogItem
from core.models.catalog_variant import CatalogVariant
from core.models.event_template import EventTemplate
from core.tests.catalog.base import AUDIT_FIELDS, CatalogTestCase

PricingType = CatalogVariant.PricingType
Unit = CatalogVariant.UnitOfMeasure
Interval = CatalogVariant.RecurringInterval
Status = CatalogVariant.AvailabilityStatus


class DefaultFlagTests(CatalogTestCase):
    def test_only_one_variant_per_item_may_be_default(self):
        item = self.make_item("Pizza")
        self.make_variant(item=item, variant_label="Small", is_default=True)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self.make_variant(item=item, variant_label="Large", is_default=True)

    def test_two_different_items_may_each_have_a_default(self):
        self.make_variant(item=self.make_item("Pizza"), is_default=True)
        self.make_variant(item=self.make_item("Pasta"), is_default=True)
        self.assertEqual(CatalogVariant.objects.filter(is_default=True).count(), 2)

    def test_deleting_the_default_promotes_the_next_variant(self):
        """Without this, "what does this cost?" comes back empty for an item that
        still has two perfectly good prices."""
        item = self.make_item("Pizza")
        small = self.make_variant(
            item=item, variant_label="Small", sort_order=1, is_default=True
        )
        large = self.make_variant(item=item, variant_label="Large", sort_order=2)

        small.soft_delete()

        large.refresh_from_db()
        self.assertTrue(large.is_default)
        self.assertFalse(CatalogVariant.all_objects.get(pk=small.pk).is_default)

    def test_deleting_a_non_default_promotes_nobody(self):
        item = self.make_item("Pizza")
        small = self.make_variant(
            item=item, variant_label="Small", sort_order=1, is_default=True
        )
        large = self.make_variant(item=item, variant_label="Large", sort_order=2)

        large.soft_delete()

        small.refresh_from_db()
        self.assertTrue(small.is_default)

    def test_deleting_the_last_variant_promotes_nobody_and_does_not_crash(self):
        item = self.make_item("Consulting")
        only = self.make_variant(item=item, is_default=True)

        only.soft_delete()

        self.assertFalse(item.variants.exists())

    def test_restoring_a_variant_does_not_steal_the_default_flag_back(self):
        """The flag is cleared on the way out precisely so the successor promoted in
        its place does not hit the partial unique constraint on restore."""
        item = self.make_item("Pizza")
        small = self.make_variant(
            item=item, variant_label="Small", sort_order=1, is_default=True
        )
        large = self.make_variant(item=item, variant_label="Large", sort_order=2)

        small.soft_delete()
        CatalogVariant.all_objects.get(pk=small.pk).restore()

        large.refresh_from_db()
        self.assertTrue(large.is_default)
        self.assertFalse(CatalogVariant.objects.get(pk=small.pk).is_default)


class SkuTests(CatalogTestCase):
    def test_duplicate_skus_collide_case_insensitively(self):
        self.make_variant(sku="ABC-1")
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self.make_variant(sku="abc-1")

    def test_the_same_sku_is_allowed_in_two_tenants(self):
        self.make_variant(sku="ABC-1")
        foreign_item = self.make_item("Coffee", tenant=self.other_tenant)
        self.make_variant(item=foreign_item, sku="ABC-1")
        self.assertEqual(CatalogVariant.objects.filter(sku="ABC-1").count(), 2)

    def test_many_variants_may_have_no_sku(self):
        item = self.make_item("Haircut")
        for index in range(3):
            self.make_variant(item=item, variant_label=f"Tier {index}")
        self.assertEqual(CatalogVariant.objects.filter(sku="").count(), 3)

    def test_barcodes_are_not_unique(self):
        """Two rows can legitimately resell the same barcoded product."""
        self.make_variant(barcode="8901234567890")
        self.make_variant(item=self.make_item("Other"), barcode="8901234567890")
        self.assertEqual(
            CatalogVariant.objects.filter(barcode="8901234567890").count(), 2
        )

    def test_restore_into_a_taken_sku_raises_validation_error(self):
        original = self.make_variant(sku="ABC-1")
        original.soft_delete()
        self.make_variant(item=self.make_item("Replacement"), sku="abc-1")

        deleted = CatalogVariant.all_objects.get(pk=original.pk)
        with self.assertRaises(ValidationError) as caught:
            deleted.restore()
        self.assertIn("sku", caught.exception.message_dict)


class MoneyBoundTests(CatalogTestCase):
    """Every assertion here goes through `update()` — see the module docstring."""

    def setUp(self):
        super().setUp()
        self.variant = self.make_variant()
        self.rows = CatalogVariant.objects.filter(pk=self.variant.pk)

    def test_a_negative_price_is_rejected_by_the_database(self):
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self.rows.update(price=Decimal("-1.00"))

    def test_a_negative_base_price_is_rejected_by_the_database(self):
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self.rows.update(base_price=Decimal("-1.00"))

    def test_a_negative_deposit_is_rejected_by_the_database(self):
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self.rows.update(deposit_amount=Decimal("-1.00"))

    def test_negative_stock_is_rejected_by_the_database(self):
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self.rows.update(track_inventory=True, stock_quantity=-1)

    def test_a_tax_rate_over_100_is_rejected_by_the_database(self):
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self.rows.update(tax_rate_percentage=Decimal("101.00"))

    def test_a_max_price_below_the_price_is_rejected(self):
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self.rows.update(
                pricing_type=PricingType.RANGE,
                price=Decimal("500.00"),
                max_price=Decimal("400.00"),
            )

    def test_a_large_price_fits(self):
        """max_digits=14, because a property in INR does not fit Plan's (10, 2)."""
        self.rows.update(price=Decimal("24000000.00"))
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.price, Decimal("24000000.00"))


class PricingCoherenceTests(CatalogTestCase):
    def _create(self, **overrides):
        return self.make_variant(**overrides)

    def test_fixed_requires_a_price(self):
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self._create(pricing_type=PricingType.FIXED, price=None)

    def test_from_requires_a_price(self):
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self._create(pricing_type=PricingType.FROM, price=None)

    def test_on_request_accepts_no_price(self):
        """The quote-based case has to work — construction, consulting, broking and
        every real-estate listing that says "price on request"."""
        variant = self._create(pricing_type=PricingType.ON_REQUEST, price=None)
        self.assertIsNone(variant.price)
        self.assertFalse(variant.is_quotable)

    def test_free_accepts_no_price(self):
        variant = self._create(pricing_type=PricingType.FREE, price=None)
        self.assertEqual(variant.display_price, "Free")

    def test_recurring_requires_an_interval(self):
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self._create(
                pricing_type=PricingType.RECURRING,
                price=Decimal("12000.00"),
                recurring_interval=None,
            )

    def test_a_non_recurring_row_may_not_carry_an_interval(self):
        """The other half of the coherence rule. Without it a FIXED price can hold a
        stale MONTH interval left over from an edit, and `display_price` has to guess
        which field to believe."""
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self._create(
                pricing_type=PricingType.FIXED,
                price=Decimal("100.00"),
                recurring_interval=Interval.MONTH,
            )

    def test_range_requires_a_max_price(self):
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self._create(
                pricing_type=PricingType.RANGE, price=Decimal("2000.00"), max_price=None
            )

    def test_rate_accepts_a_percentage(self):
        variant = self._create(
            pricing_type=PricingType.RATE,
            price=Decimal("8.40"),
            unit_of_measure=Unit.PERCENT,
            unit_label="per annum on the loan amount",
        )
        self.assertEqual(variant.price, Decimal("8.40"))

    def test_a_rate_over_100_is_rejected(self):
        """A percentage is bounded even though a price is not. 840% is a typo that
        would otherwise be read out to a caller as fact."""
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self._create(
                pricing_type=PricingType.RATE,
                price=Decimal("840.00"),
                unit_of_measure=Unit.PERCENT,
            )

    def test_a_price_over_100_is_fine_when_it_is_not_a_rate(self):
        variant = self._create(pricing_type=PricingType.FIXED, price=Decimal("840.00"))
        self.assertEqual(variant.price, Decimal("840.00"))

    def test_a_composite_price_stores_both_components(self):
        variant = self._create(
            pricing_type=PricingType.FIXED,
            base_price=Decimal("50.00"),
            price=Decimal("18.00"),
            unit_of_measure=Unit.KM,
        )
        self.assertEqual(variant.base_price, Decimal("50.00"))
        self.assertEqual(variant.price, Decimal("18.00"))


class CleanRuleTests(CatalogTestCase):
    def test_a_cross_tenant_item_is_rejected(self):
        foreign_item = self.make_item("Coffee", tenant=self.other_tenant)
        variant = CatalogVariant(
            tenant=self.tenant,
            item=foreign_item,
            variant_label="Standard",
            price=Decimal("10.00"),
        )
        with self.assertRaises(ValidationError) as caught:
            variant.full_clean(exclude=AUDIT_FIELDS)
        self.assertIn("item", caught.exception.message_dict)

    def test_duration_is_refused_when_the_item_is_booked_by_template(self):
        """One source of truth for duration. Two that can disagree surface as a
        double-booked host."""
        template = EventTemplate.objects.create(
            tenant=self.tenant, name="Cleaning", duration_minutes=30
        )
        item = self.make_item(
            "Dental Cleaning",
            item_kind=CatalogItem.ItemKind.SERVICE,
            event_template=template,
        )
        variant = CatalogVariant(
            tenant=self.tenant,
            item=item,
            variant_label="Standard",
            price=Decimal("1500.00"),
            duration_minutes=45,
        )
        with self.assertRaises(ValidationError) as caught:
            variant.full_clean(exclude=AUDIT_FIELDS)
        self.assertIn("duration_minutes", caught.exception.message_dict)

    def test_duration_is_allowed_when_there_is_no_template(self):
        """A barber listing "Haircut — 30 min" without configuring a bookable event
        type is the reason this column exists."""
        item = self.make_item("Haircut", item_kind=CatalogItem.ItemKind.SERVICE)
        variant = CatalogVariant(
            tenant=self.tenant,
            item=item,
            variant_label="Adult",
            price=Decimal("300.00"),
            duration_minutes=30,
        )
        variant.full_clean(exclude=AUDIT_FIELDS)

    def test_stock_without_tracking_is_rejected(self):
        """One representation only — nothing may be both untracked and holding a
        count, or "is it in stock?" has two answers."""
        item = self.make_item("Haircut")
        variant = CatalogVariant(
            tenant=self.tenant,
            item=item,
            variant_label="Standard",
            price=Decimal("10.00"),
            track_inventory=False,
            stock_quantity=5,
        )
        with self.assertRaises(ValidationError) as caught:
            variant.full_clean(exclude=AUDIT_FIELDS)
        self.assertIn("stock_quantity", caught.exception.message_dict)

    def test_a_blank_variant_reports_the_missing_item_rather_than_crashing(self):
        """`item` is non-nullable, so `self.item` raises rather than returning None.
        clean() runs even after clean_fields() has recorded the error, so an
        unguarded access here is a 500 on an empty admin form."""
        variant = CatalogVariant(tenant=self.tenant, price=Decimal("10.00"))
        with self.assertRaises(ValidationError) as caught:
            variant.full_clean(exclude=AUDIT_FIELDS)
        self.assertIn("item", caught.exception.message_dict)

    def test_a_lowercase_currency_is_canonicalised_on_save(self):
        """`inr` and `INR` are the same currency, so the column stores one of them.

        Normalised in `save()` rather than rejected, and the field's regex is
        case-insensitive to match. A strict `^[A-Z]{3}$` runs during DRF's *field*
        validation — before the serializer's `validate_currency` can normalise — which
        made the API reject "usd" while the shell stored it verbatim.
        """
        variant = self.make_variant(currency="usd")
        self.assertEqual(variant.currency, "USD")

        variant.currency = "  eur "
        variant.save(update_fields=["currency"])
        variant.refresh_from_db()
        self.assertEqual(variant.currency, "EUR")

    def test_a_malformed_currency_is_still_rejected(self):
        for value in ("IN", "INRR", "IN1"):
            with self.subTest(value=value):
                variant = self.make_variant()
                variant.currency = value
                with self.assertRaises(ValidationError) as caught:
                    variant.full_clean(exclude=AUDIT_FIELDS)
                self.assertIn("currency", caught.exception.message_dict)

    def test_the_currency_defaults_from_settings(self):
        self.assertEqual(self.make_variant().currency, "INR")

    def test_the_currency_may_differ_between_variants(self):
        """No cross-variant consistency constraint: an export business quoting one
        line in USD is not a data error."""
        item = self.make_item("Spice Mix")
        self.make_variant(item=item, variant_label="Domestic", currency="INR")
        self.make_variant(item=item, variant_label="Export", currency="USD")
        self.assertEqual(item.variants.count(), 2)


class SellabilityTests(CatalogTestCase):
    def test_an_ordinary_variant_is_sellable(self):
        self.assertTrue(self.make_variant().is_sellable)

    def test_an_inactive_variant_is_not(self):
        self.assertFalse(self.make_variant(is_active=False).is_sellable)

    def test_out_of_stock_is_not(self):
        self.assertFalse(
            self.make_variant(availability_status=Status.OUT_OF_STOCK).is_sellable
        )

    def test_discontinued_is_not(self):
        self.assertFalse(
            self.make_variant(availability_status=Status.DISCONTINUED).is_sellable
        )

    def test_tracked_and_zero_is_not(self):
        self.assertFalse(
            self.make_variant(track_inventory=True, stock_quantity=0).is_sellable
        )

    def test_tracked_and_positive_is(self):
        self.assertTrue(
            self.make_variant(track_inventory=True, stock_quantity=4).is_sellable
        )

    def test_untracked_and_zero_stock_is_still_sellable(self):
        """The reason `track_inventory` defaults to False: a haircut has no count,
        and reading 0 as "out of stock" would delist every service."""
        self.assertTrue(self.make_variant(track_inventory=False).is_sellable)

    def test_made_to_order_is_sellable(self):
        self.assertTrue(
            self.make_variant(availability_status=Status.MADE_TO_ORDER).is_sellable
        )

    def test_an_inactive_item_makes_its_variants_unsellable(self):
        """`is_sellable` is meant to be the single definition, so it has to consider
        the parent — otherwise every consumer has to remember to check it too, and
        one of them will not."""
        item = self.make_item("Pizza", is_active=False)
        self.assertFalse(self.make_variant(item=item).is_sellable)

    def test_an_item_outside_its_season_is_not_sellable(self):
        item = self.make_item(
            "Mango Shake",
            available_from=date(2020, 4, 1),
            available_until=date(2020, 6, 30),
        )
        self.assertFalse(self.make_variant(item=item).is_sellable)

    def test_an_item_inside_its_season_is_sellable(self):
        item = self.make_item(
            "Mango Shake",
            available_from=date(2020, 4, 1),
            available_until=date(2099, 6, 30),
        )
        self.assertTrue(self.make_variant(item=item).is_sellable)

    def test_an_open_ended_season_is_sellable(self):
        item = self.make_item("Mango Shake", available_from=date(2020, 4, 1))
        self.assertTrue(self.make_variant(item=item).is_sellable)


class DisplayPriceTests(CatalogTestCase):
    """One rendering for every consumer. The composite and rate shapes are where two
    independent implementations would drift, which is why they are tested."""

    def test_a_fixed_price_drops_the_trailing_zeros(self):
        variant = self.make_variant(price=Decimal("2500.00"))
        self.assertEqual(variant.display_price, "₹2,500")

    def test_a_fixed_price_keeps_real_paise(self):
        variant = self.make_variant(price=Decimal("99.50"))
        self.assertEqual(variant.display_price, "₹99.50")

    def test_a_unit_price_names_its_unit(self):
        variant = self.make_variant(price=Decimal("2500.00"), unit_of_measure=Unit.HOUR)
        self.assertEqual(variant.display_price, "₹2,500 per hour")

    def test_a_unit_label_overrides_the_unit_name(self):
        variant = self.make_variant(
            price=Decimal("380.00"), unit_of_measure=Unit.KG, unit_label="per 50 kg bag"
        )
        self.assertEqual(variant.display_price, "₹380 per 50 kg bag")

    def test_a_piece_price_says_nothing_about_units(self):
        variant = self.make_variant(price=Decimal("380.00"), unit_of_measure=Unit.PIECE)
        self.assertEqual(variant.display_price, "₹380")

    def test_a_composite_price_shows_both_components(self):
        variant = self.make_variant(
            base_price=Decimal("50.00"),
            price=Decimal("18.00"),
            unit_of_measure=Unit.KM,
        )
        self.assertEqual(variant.display_price, "₹50 base + ₹18 per kilometre")

    def test_a_from_price_is_marked_as_a_floor(self):
        variant = self.make_variant(
            pricing_type=PricingType.FROM, price=Decimal("50000.00")
        )
        self.assertEqual(variant.display_price, "from ₹50,000")

    def test_a_range_shows_both_ends(self):
        variant = self.make_variant(
            pricing_type=PricingType.RANGE,
            price=Decimal("2000.00"),
            max_price=Decimal("5000.00"),
        )
        self.assertEqual(variant.display_price, "₹2,000–₹5,000")

    def test_a_recurring_price_names_its_interval(self):
        variant = self.make_variant(
            pricing_type=PricingType.RECURRING,
            price=Decimal("12000.00"),
            recurring_interval=Interval.YEAR,
        )
        self.assertEqual(variant.display_price, "₹12,000 per year")

    def test_a_multi_interval_recurring_price_pluralises(self):
        variant = self.make_variant(
            pricing_type=PricingType.RECURRING,
            price=Decimal("3000.00"),
            recurring_interval=Interval.MONTH,
            recurring_interval_count=3,
        )
        self.assertEqual(variant.display_price, "₹3,000 per 3 months")

    def test_a_rate_reads_as_a_percentage(self):
        variant = self.make_variant(
            pricing_type=PricingType.RATE,
            price=Decimal("8.40"),
            unit_of_measure=Unit.PERCENT,
            unit_label="per annum on the loan amount",
        )
        self.assertEqual(variant.display_price, "8.4% per annum on the loan amount")

    def test_on_request_says_so(self):
        variant = self.make_variant(
            pricing_type=PricingType.ON_REQUEST, price=None
        )
        self.assertEqual(variant.display_price, "Price on request")

    def test_an_unknown_currency_falls_back_to_its_code(self):
        variant = self.make_variant(price=Decimal("500.00"), currency="AED")
        self.assertEqual(variant.display_price, "AED 500")
