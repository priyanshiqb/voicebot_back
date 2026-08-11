"""
Variant API — the pricing table.

The behaviour unique to this viewset is **default-flag management**. A partial unique
constraint permits one default per item, so writing a second one raises
`IntegrityError` unless the current holder is demoted first. Making the client do that
in two requests is a race that ends with an item having none or two.

So `perform_create`, `perform_update` and `set-default` all demote inside one
transaction, and the tests below check each entry point separately — they are three
code paths, and a fix applied to one of them is easy to forget in the others.
"""

from decimal import Decimal

from core.models.catalog_variant import CatalogVariant
from core.tests.catalog.views.base import VARIANTS, CatalogAPITestCase

PricingType = CatalogVariant.PricingType
Unit = CatalogVariant.UnitOfMeasure
Interval = CatalogVariant.RecurringInterval
Status = CatalogVariant.AvailabilityStatus


class VariantAPITestCase(CatalogAPITestCase):
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


class AuthTests(VariantAPITestCase):
    def test_anonymous_is_refused(self):
        self.assertEqual(self.api_anon.get(VARIANTS).status_code, 401)

    def test_a_machine_token_is_refused(self):
        self.assertEqual(self.api_machine.get(VARIANTS).status_code, 403)


class CrudTests(VariantAPITestCase):
    def test_create_returns_the_rendered_price(self):
        data = self.assert_ok(
            self.api.post(VARIANTS, self.payload(), format="json"), 201
        )
        self.assertEqual(data["display_price"], "₹100")
        self.assertEqual(data["item_name"], "Pizza")
        self.assertTrue(data["is_sellable"])

    def test_create_stamps_the_tenant_and_audit_user(self):
        data = self.assert_ok(
            self.api.post(VARIANTS, self.payload(), format="json"), 201
        )
        variant = CatalogVariant.objects.get(pk=data["id"])
        self.assertEqual(variant.tenant_id, self.tenant.id)
        self.assertEqual(variant.created_by, self.user)

    def test_repricing_is_a_one_field_patch(self):
        variant = self.make_variant(item=self.item)
        data = self.assert_ok(
            self.api.patch(
                f"{VARIANTS}{variant.pk}/", {"price": "125.00"}, format="json"
            )
        )
        self.assertEqual(data["display_price"], "₹125")

    def test_a_broken_pricing_rule_is_a_400_on_the_field(self):
        self.assert_error(
            self.api.post(
                VARIANTS,
                self.payload(pricing_type=PricingType.RECURRING, price="12000.00"),
                format="json",
            ),
            field="recurring_interval",
            contains="needs an interval",
        )

    def test_a_duplicate_sku_is_a_400_on_sku(self):
        self.make_variant(item=self.item, variant_label="A", sku="PZ-1")
        self.assert_error(
            self.api.post(
                VARIANTS, self.payload(variant_label="B", sku="pz-1"), format="json"
            ),
            field="sku",
        )

    def test_a_composite_price_round_trips(self):
        data = self.assert_ok(
            self.api.post(
                VARIANTS,
                self.payload(base_price="50.00", price="18.00", unit_of_measure=Unit.KM),
                format="json",
            ),
            201,
        )
        self.assertEqual(data["display_price"], "₹50 base + ₹18 per kilometre")

    def test_a_rate_price_round_trips(self):
        data = self.assert_ok(
            self.api.post(
                VARIANTS,
                self.payload(
                    pricing_type=PricingType.RATE,
                    price="8.40",
                    unit_of_measure=Unit.PERCENT,
                    unit_label="per annum on the loan amount",
                ),
                format="json",
            ),
            201,
        )
        self.assertEqual(data["display_price"], "8.4% per annum on the loan amount")

    def test_a_lowercase_currency_is_accepted_and_canonicalised(self):
        """Regression: the model's strict `^[A-Z]{3}$` ran during DRF field validation,
        before `validate_currency` could normalise, so "usd" was a 400."""
        data = self.assert_ok(
            self.api.post(VARIANTS, self.payload(currency="usd"), format="json"), 201
        )
        self.assertEqual(data["currency"], "USD")


class DefaultFlagTests(VariantAPITestCase):
    def test_creating_a_second_default_demotes_the_first(self):
        first = self.make_variant(item=self.item, variant_label="A", is_default=True)

        self.assert_ok(
            self.api.post(
                VARIANTS,
                self.payload(variant_label="B", is_default=True),
                format="json",
            ),
            201,
        )

        first.refresh_from_db()
        self.assertFalse(first.is_default)
        self.assertEqual(
            CatalogVariant.objects.filter(item=self.item, is_default=True).count(), 1
        )

    def test_patching_is_default_demotes_the_incumbent(self):
        first = self.make_variant(item=self.item, variant_label="A", is_default=True)
        second = self.make_variant(item=self.item, variant_label="B")

        self.assert_ok(
            self.api.patch(
                f"{VARIANTS}{second.pk}/", {"is_default": True}, format="json"
            )
        )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertFalse(first.is_default)
        self.assertTrue(second.is_default)

    def test_set_default_promotes_and_demotes(self):
        first = self.make_variant(item=self.item, variant_label="A", is_default=True)
        second = self.make_variant(item=self.item, variant_label="B")

        data = self.assert_ok(self.api.post(f"{VARIANTS}{second.pk}/set-default/"))

        self.assertTrue(data["is_default"])
        first.refresh_from_db()
        self.assertFalse(first.is_default)

    def test_set_default_is_idempotent(self):
        variant = self.make_variant(item=self.item, is_default=True)

        self.assert_ok(self.api.post(f"{VARIANTS}{variant.pk}/set-default/"))
        self.assert_ok(self.api.post(f"{VARIANTS}{variant.pk}/set-default/"))

        self.assertEqual(
            CatalogVariant.objects.filter(item=self.item, is_default=True).count(), 1
        )

    def test_set_default_records_the_updater(self):
        variant = self.make_variant(item=self.item)
        self.assert_ok(self.api.post(f"{VARIANTS}{variant.pk}/set-default/"))
        variant.refresh_from_db()
        self.assertEqual(variant.updated_by, self.user)

    def test_set_default_on_another_tenants_variant_is_404(self):
        foreign_item = self.make_item("Theirs", tenant=self.other_tenant)
        foreign = self.make_variant(item=foreign_item)
        self.assertEqual(
            self.api.post(f"{VARIANTS}{foreign.pk}/set-default/").status_code, 404
        )

    def test_defaults_in_two_items_do_not_interfere(self):
        other_item = self.make_item("Pasta")
        first = self.make_variant(item=self.item, is_default=True)
        second = self.make_variant(item=other_item, is_default=True)

        self.assert_ok(self.api.post(f"{VARIANTS}{second.pk}/set-default/"))

        first.refresh_from_db()
        self.assertTrue(first.is_default)

    def test_deleting_the_default_promotes_a_successor(self):
        """Otherwise the item answers "what does this cost?" with nothing while still
        holding two perfectly good prices."""
        first = self.make_variant(
            item=self.item, variant_label="A", sort_order=0, is_default=True
        )
        second = self.make_variant(item=self.item, variant_label="B", sort_order=1)

        self.assertEqual(self.api.delete(f"{VARIANTS}{first.pk}/").status_code, 200)

        second.refresh_from_db()
        self.assertTrue(second.is_default)


class ListingTests(VariantAPITestCase):
    def setUp(self):
        super().setUp()
        self.cheap = self.make_variant(
            item=self.item,
            variant_label="Small",
            price=Decimal("200.00"),
            sku="PZ-S",
            is_default=True,
            track_inventory=True,
            stock_quantity=2,
        )
        self.service_item = self.make_item("Consulting", item_kind="SERVICE")
        self.expensive = self.make_variant(
            item=self.service_item,
            variant_label="Retainer",
            price=Decimal("5000.00"),
            pricing_type=PricingType.RECURRING,
            recurring_interval=Interval.MONTH,
            availability_status=Status.OUT_OF_STOCK,
        )

    def test_filter_by_item(self):
        rows = self.assert_paginated(
            self.api.get(VARIANTS, {"item": str(self.item.pk)}), count=1
        )
        self.assertEqual(rows[0]["variant_label"], "Small")

    def test_filter_by_item_kind_through_the_relation(self):
        self.assert_paginated(
            self.api.get(VARIANTS, {"item__item_kind": "SERVICE"}), count=1
        )

    def test_filter_by_pricing_type_in(self):
        self.assert_paginated(
            self.api.get(VARIANTS, {"pricing_type__in": "RECURRING,RATE"}), count=1
        )

    def test_filter_by_availability_status(self):
        rows = self.assert_paginated(
            self.api.get(VARIANTS, {"availability_status": "OUT_OF_STOCK"}), count=1
        )
        self.assertEqual(rows[0]["variant_label"], "Retainer")

    def test_filter_the_low_stock_list(self):
        rows = self.assert_paginated(
            self.api.get(
                VARIANTS, {"track_inventory": "true", "stock_quantity__lte": 5}
            ),
            count=1,
        )
        self.assertEqual(rows[0]["variant_label"], "Small")

    def test_filter_by_price_range(self):
        self.assert_paginated(self.api.get(VARIANTS, {"price__gte": "1000"}), count=1)
        self.assert_paginated(self.api.get(VARIANTS, {"price__lte": "1000"}), count=1)

    def test_filter_by_is_default(self):
        self.assert_paginated(self.api.get(VARIANTS, {"is_default": "true"}), count=1)

    def test_filter_by_currency(self):
        self.assert_paginated(self.api.get(VARIANTS, {"currency": "INR"}), count=2)

    def test_search_matches_sku_and_the_parent_item_name(self):
        self.assert_paginated(self.api.get(VARIANTS, {"search": "PZ-S"}), count=1)
        self.assert_paginated(self.api.get(VARIANTS, {"search": "Consulting"}), count=1)

    def test_ordering_by_price(self):
        rows = self.assert_paginated(self.api.get(VARIANTS, {"ordering": "-price"}))
        self.assertEqual(rows[0]["variant_label"], "Retainer")

    def test_the_list_row_carries_the_rendered_price_and_sellability(self):
        rows = self.assert_paginated(
            self.api.get(VARIANTS, {"item": str(self.item.pk)})
        )
        row = rows[0]
        self.assertEqual(row["display_price"], "₹200")
        self.assertTrue(row["is_sellable"])
        self.assertEqual(row["item_name"], "Pizza")
        for absent in ("created_by", "updated_by", "tenant"):
            self.assertNotIn(absent, row)

    def test_out_of_stock_reads_as_unsellable(self):
        rows = self.assert_paginated(
            self.api.get(VARIANTS, {"availability_status": "OUT_OF_STOCK"})
        )
        self.assertFalse(rows[0]["is_sellable"])

    def test_the_dropdown_label_carries_the_price(self):
        rows = self.assert_paginated(self.api.get(f"{VARIANTS}dropdown/"))
        labels = {row["label"] for row in rows}
        self.assertIn("Small — ₹200", labels)


class TenantIsolationTests(VariantAPITestCase):
    def setUp(self):
        super().setUp()
        self.mine = self.make_variant(item=self.item)
        foreign_item = self.make_item("Theirs", tenant=self.other_tenant)
        self.theirs = self.make_variant(item=foreign_item)

    def test_the_list_shows_only_the_callers_tenant(self):
        rows = self.assert_paginated(self.api.get(VARIANTS), count=1)
        self.assertEqual(rows[0]["id"], str(self.mine.pk))

    def test_another_tenants_variant_is_404_on_every_verb(self):
        for method, kwargs in (
            ("get", {}),
            ("patch", {"data": {"price": "1.00"}, "format": "json"}),
            ("delete", {}),
        ):
            with self.subTest(method=method):
                response = getattr(self.api, method)(
                    f"{VARIANTS}{self.theirs.pk}/", **kwargs
                )
                self.assertEqual(response.status_code, 404, response.data)

    def test_creating_a_variant_on_another_tenants_item_is_a_400(self):
        foreign_item = self.make_item("Also Theirs", tenant=self.other_tenant)
        self.assert_error(
            self.api.post(
                VARIANTS,
                {
                    "item": str(foreign_item.pk),
                    "variant_label": "Sneaky",
                    "price": "1.00",
                },
                format="json",
            ),
            field="item",
            contains="different tenant",
        )
