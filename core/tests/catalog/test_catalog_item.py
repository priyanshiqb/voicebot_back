"""
The catalog item — the nameable half of an offering.

The rule most likely to be "fixed" by someone who has not read the plan is that
`name` is **not** unique. Every other named model in this app (Agent, EventTemplate,
Campaign, KnowledgeBase) has a partial unique constraint on its name, so the absence
of one here reads like an oversight. It is not: those are configuration objects a user
names, and a catalog is a data set where two brands of "Basmati Rice 5 kg" is a
correct pair of rows. `test_two_items_may_share_a_name` exists to fail loudly if
someone adds the constraint.
"""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.fields import NOT_PROVIDED

from core.models.catalog_item import CatalogItem
from core.models.catalog_item_media import CatalogItemMedia
from core.models.catalog_variant import CatalogVariant
from core.models.event_template import EventTemplate
from core.tests.catalog.base import AUDIT_FIELDS, CatalogTestCase


class CreationTests(CatalogTestCase):
    def test_an_item_needs_only_a_kind_and_a_name(self):
        """The "tenant adds their first product before designing a taxonomy" path.
        A required category would make this impossible."""
        item = CatalogItem.objects.create(
            tenant=self.tenant,
            item_kind=CatalogItem.ItemKind.SERVICE,
            name="Haircut",
        )
        item.full_clean(exclude=AUDIT_FIELDS)

        self.assertIsNone(item.category)
        self.assertEqual(item.tags, [])
        self.assertEqual(item.aliases, [])
        self.assertEqual(item.fulfilment_modes, [])
        self.assertEqual(item.attributes, {})

    def test_item_kind_has_no_default(self):
        """Forcing the choice is what stops a catalog filing everything under
        whichever value happened to be the default."""
        self.assertIs(
            CatalogItem._meta.get_field("item_kind").default, NOT_PROVIDED
        )

    def test_two_items_may_share_a_name(self):
        self.make_item("Basmati Rice 5 kg", brand="Brand A")
        self.make_item("Basmati Rice 5 kg", brand="Brand B")
        self.assertEqual(
            CatalogItem.objects.filter(name="Basmati Rice 5 kg").count(), 2
        )


class ItemCodeTests(CatalogTestCase):
    def test_duplicate_item_codes_collide_case_insensitively(self):
        self.make_item("First", item_code="PRD-1024")
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self.make_item("Second", item_code="prd-1024")

    def test_the_same_item_code_is_allowed_in_two_tenants(self):
        self.make_item("First", item_code="PRD-1024")
        self.make_item("Second", tenant=self.other_tenant, item_code="PRD-1024")
        self.assertEqual(CatalogItem.objects.filter(item_code="PRD-1024").count(), 2)

    def test_many_items_may_have_no_item_code(self):
        """The blank-excluding half of the constraint is what makes the field
        optional at all — under a plain unique constraint two empty strings collide
        and the second service business cannot add a second service."""
        for index in range(3):
            self.make_item(f"Service {index}")
        self.assertEqual(CatalogItem.objects.filter(item_code="").count(), 3)

    def test_soft_deleting_frees_the_item_code(self):
        original = self.make_item("First", item_code="PRD-1")
        original.soft_delete()
        self.make_item("Replacement", item_code="PRD-1")

        self.assertEqual(CatalogItem.objects.filter(item_code="PRD-1").count(), 1)

    def test_restore_into_a_taken_item_code_raises_validation_error(self):
        original = self.make_item("First", item_code="PRD-1")
        original.soft_delete()
        self.make_item("Replacement", item_code="prd-1")

        deleted = CatalogItem.all_objects.get(pk=original.pk)
        with self.assertRaises(ValidationError) as caught:
            deleted.restore()
        self.assertIn("item_code", caught.exception.message_dict)

    def test_an_item_without_a_code_always_restores(self):
        item = self.make_item("Service")
        item.soft_delete()

        CatalogItem.all_objects.get(pk=item.pk).restore()
        self.assertTrue(CatalogItem.objects.filter(pk=item.pk).exists())


class RelationTests(CatalogTestCase):
    def _event_template(self, tenant=None):
        return EventTemplate.objects.create(
            tenant=tenant or self.tenant,
            name="Dental Cleaning",
            duration_minutes=30,
        )

    def test_a_cross_tenant_category_is_rejected(self):
        foreign = self.make_category("Menu", tenant=self.other_tenant)
        item = CatalogItem(
            tenant=self.tenant,
            item_kind=CatalogItem.ItemKind.PHYSICAL_GOOD,
            name="Coffee",
            category=foreign,
        )
        with self.assertRaises(ValidationError) as caught:
            item.full_clean(exclude=AUDIT_FIELDS)
        self.assertIn("category", caught.exception.message_dict)

    def test_a_cross_tenant_event_template_is_rejected(self):
        item = CatalogItem(
            tenant=self.tenant,
            item_kind=CatalogItem.ItemKind.SERVICE,
            name="Cleaning",
            event_template=self._event_template(tenant=self.other_tenant),
        )
        with self.assertRaises(ValidationError) as caught:
            item.full_clean(exclude=AUDIT_FIELDS)
        self.assertIn("event_template", caught.exception.message_dict)

    def test_only_a_service_can_be_bookable(self):
        item = CatalogItem(
            tenant=self.tenant,
            item_kind=CatalogItem.ItemKind.PHYSICAL_GOOD,
            name="Fertilizer",
            event_template=self._event_template(),
        )
        with self.assertRaises(ValidationError) as caught:
            item.full_clean(exclude=AUDIT_FIELDS)
        self.assertIn("event_template", caught.exception.message_dict)

    def test_a_service_with_an_event_template_validates(self):
        item = CatalogItem(
            tenant=self.tenant,
            item_kind=CatalogItem.ItemKind.SERVICE,
            name="Dental Cleaning",
            event_template=self._event_template(),
        )
        item.full_clean(exclude=AUDIT_FIELDS)

    def test_deleting_a_category_orphans_its_items_rather_than_them(self):
        category = self.make_category("Menu")
        item = self.make_item(category=category)

        category.items.all().update(category=None)
        category.delete()

        item.refresh_from_db()
        self.assertIsNone(item.category_id)


class AvailabilityWindowTests(CatalogTestCase):
    def test_a_reversed_window_is_rejected_by_the_database(self):
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self.make_item(
                "Mango Shake", available_from="2026-06-30", available_until="2026-04-01"
            )

    def test_an_ordered_window_is_accepted(self):
        item = self.make_item(
            "Mango Shake", available_from="2026-04-01", available_until="2026-06-30"
        )
        self.assertIsNotNone(item.available_from)

    def test_one_open_end_is_allowed(self):
        self.make_item("From April", available_from="2026-04-01")
        self.make_item("Until June", available_until="2026-06-30")


class AttributeTests(CatalogTestCase):
    def test_nested_attributes_round_trip(self):
        item = self.make_item(
            "3 BHK Andheri West",
            attributes={
                "bhk": 3,
                "carpet_area_sqft": 1150,
                "furnishing": "semi",
                "amenities": ["lift", "parking"],
            },
        )
        item.refresh_from_db()
        self.assertEqual(item.attributes["bhk"], 3)
        self.assertEqual(item.attributes["amenities"], ["lift", "parking"])

    def test_attributes_are_queryable_by_key(self):
        """The whole justification for JSON over EAV is that Postgres can still
        filter it. If this breaks, the design argument breaks with it."""
        self.make_item("3 BHK", attributes={"bhk": 3})
        self.make_item("2 BHK", attributes={"bhk": 2})

        self.assertEqual(CatalogItem.objects.filter(attributes__bhk=3).count(), 1)

    def test_tags_and_aliases_are_queryable(self):
        self.make_item("Coca-Cola 500 ml", tags=["bestseller"], aliases=["coke", "cola"])

        self.assertTrue(
            CatalogItem.objects.filter(tags__contains=["bestseller"]).exists()
        )
        self.assertTrue(CatalogItem.objects.filter(aliases__contains=["coke"]).exists())

    def test_a_fulfilment_mode_outside_the_choices_is_rejected(self):
        item = self.make_item("Thali", fulfilment_modes=["DINE_IN", "TELEPORT"])
        with self.assertRaises(ValidationError) as caught:
            item.full_clean(exclude=AUDIT_FIELDS)
        self.assertIn("fulfilment_modes", caught.exception.message_dict)

    def test_several_fulfilment_modes_are_allowed(self):
        item = self.make_item(
            "Thali",
            fulfilment_modes=[
                CatalogItem.FulfilmentMode.DINE_IN,
                CatalogItem.FulfilmentMode.DELIVERY,
                CatalogItem.FulfilmentMode.PICKUP,
            ],
        )
        item.full_clean(exclude=AUDIT_FIELDS)
        self.assertEqual(len(item.fulfilment_modes), 3)


class DefaultVariantTests(CatalogTestCase):
    def test_a_bare_item_gets_a_standard_on_request_variant(self):
        """ON_REQUEST, not a zero price. "We have not priced this yet" is the truth;
        FREE or FIXED 0.00 would have the agent quote it as free."""
        item = self.make_item("Consulting")

        variant = item.ensure_default_variant()

        self.assertEqual(variant.variant_label, "Standard")
        self.assertTrue(variant.is_default)
        self.assertEqual(variant.pricing_type, CatalogVariant.PricingType.ON_REQUEST)
        self.assertIsNone(variant.price)
        self.assertEqual(variant.tenant_id, item.tenant_id)

    def test_an_existing_variant_is_promoted_rather_than_duplicated(self):
        """The bug this prevents: an API that writes three variants and then asks for
        a default ending up with four, one of them junk."""
        item = self.make_item("Pizza")
        small = self.make_variant(item=item, variant_label="Small", sort_order=1)
        self.make_variant(item=item, variant_label="Large", sort_order=2)

        promoted = item.ensure_default_variant()

        self.assertEqual(promoted.pk, small.pk)
        self.assertEqual(item.variants.count(), 2)

    def test_it_is_idempotent(self):
        item = self.make_item("Consulting")
        first = item.ensure_default_variant()
        second = item.ensure_default_variant()

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(item.variants.count(), 1)

    def test_default_variant_property_reads_the_flagged_row(self):
        item = self.make_item("Pizza")
        self.make_variant(item=item, variant_label="Small", sort_order=1)
        large = self.make_variant(
            item=item, variant_label="Large", sort_order=2, is_default=True
        )

        self.assertEqual(item.default_variant.pk, large.pk)


class CascadeTests(CatalogTestCase):
    def test_soft_deleting_an_item_takes_its_variants_and_media(self):
        """A variant of a delisted item left active is exactly the orphan that gets
        quoted at a caller months later."""
        item = self.make_item("Pizza")
        variant = self.make_variant(item=item, is_default=True)
        media = CatalogItemMedia.objects.create(
            tenant=self.tenant, item=item, original_name="pizza.jpg", is_primary=True
        )

        item.soft_delete()

        self.assertTrue(CatalogVariant.all_objects.get(pk=variant.pk).is_deleted)
        self.assertTrue(CatalogItemMedia.all_objects.get(pk=media.pk).is_deleted)
        self.assertFalse(item.variants.exists())

    def test_hard_deleting_an_item_cascades_its_variants(self):
        item = self.make_item("Pizza")
        variant = self.make_variant(item=item)

        CatalogItem.all_objects.filter(pk=item.pk).delete()

        self.assertFalse(CatalogVariant.all_objects.filter(pk=variant.pk).exists())
