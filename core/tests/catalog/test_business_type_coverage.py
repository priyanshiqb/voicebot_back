"""
The acceptance test for "covers all types of business product and service".

Data-driven on purpose. The claim this module makes is about *breadth*, and breadth
is not something a hand-written assertion per rule can demonstrate — it needs one
fixture per business, each constructed the way that business would actually be
listed. Adding a vertical later is appending a dict, and a regression reads as
*"the insurance row no longer builds"* rather than as a failed assertion with no
business attached to it.

Each case asserts three things: the row constructs, it passes `full_clean()`, and its
default variant renders a price a voice agent could read out loud.

Not covered, by decision rather than oversight: **date-priced inventory** — hotels,
homestays, cinemas, ticketed events. Those need a rate per calendar date and per-date
capacity, which is a fifth table this product will not build. The neighbouring cases
that look similar (gym peak/off-peak, tour season pricing) *are* covered, as fixed
rate buckets, and are in the list below so the dividing line is visible in the tests
rather than only in the plan.
"""

from core.models.catalog_item import CatalogItem
from core.models.catalog_variant import CatalogVariant
from core.models.event_template import EventTemplate
from core.tests.catalog.base import AUDIT_FIELDS, CatalogTestCase

Kind = CatalogItem.ItemKind
Mode = CatalogItem.FulfilmentMode
PricingType = CatalogVariant.PricingType
Unit = CatalogVariant.UnitOfMeasure
Interval = CatalogVariant.RecurringInterval
Status = CatalogVariant.AvailabilityStatus


#: (business, item kwargs, [variant kwargs], expected display price of the default)
BUSINESS_CASES = [
    (
        "Retail / e-commerce",
        {"name": "Nike Air Max", "item_kind": Kind.PHYSICAL_GOOD, "brand": "Nike"},
        [
            {
                "variant_label": "UK 8",
                "sku": "NAM-8",
                "price": "8995.00",
                "track_inventory": True,
                "stock_quantity": 4,
                "is_default": True,
            },
            {
                "variant_label": "UK 9",
                "sku": "NAM-9",
                "price": "8995.00",
                "track_inventory": True,
                "stock_quantity": 0,
                "availability_status": Status.OUT_OF_STOCK,
            },
        ],
        "₹8,995",
    ),
    (
        "Restaurant",
        {
            "name": "Paneer Butter Masala",
            "item_kind": Kind.PHYSICAL_GOOD,
            "attributes": {"veg": True, "spice": "medium", "allergens": ["dairy"]},
            "fulfilment_modes": [Mode.DINE_IN, Mode.DELIVERY, Mode.PICKUP],
            "lead_time_minutes": 20,
        },
        [
            {"variant_label": "Half", "price": "220.00", "is_default": True},
            {"variant_label": "Full", "price": "380.00", "sort_order": 1},
        ],
        "₹220",
    ),
    (
        "Agricultural supplier",
        {
            "name": "Urea Fertilizer",
            "item_kind": Kind.PHYSICAL_GOOD,
            "attributes": {"nutrient": "N", "grade": "46-0-0"},
        },
        [
            {
                "variant_label": "1 kg pouch",
                "price": "32.00",
                "unit_of_measure": Unit.KG,
                "unit_label": "per 1 kg pouch",
                "tax_code": "31021000",
                "is_default": True,
            },
            {
                "variant_label": "50 kg bag",
                "price": "1350.00",
                "unit_of_measure": Unit.KG,
                "unit_label": "per 50 kg bag",
                "tax_code": "31021000",
                "track_inventory": True,
                "stock_quantity": 120,
                "sort_order": 1,
            },
        ],
        "₹32 per 1 kg pouch",
    ),
    (
        "Real estate — sale",
        {
            "name": "3 BHK, Andheri West",
            "item_kind": Kind.PHYSICAL_GOOD,
            "attributes": {
                "bhk": 3,
                "carpet_area_sqft": 1150,
                "furnishing": "semi",
                "floor": 11,
            },
        },
        [{"variant_label": "Outright", "price": "24000000.00", "is_default": True}],
        "₹24,000,000",
    ),
    (
        "Real estate — rent",
        {
            "name": "2 BHK, Powai",
            "item_kind": Kind.PHYSICAL_GOOD,
            "attributes": {"bhk": 2, "furnishing": "full"},
        },
        [
            {
                "variant_label": "Monthly rent",
                "pricing_type": PricingType.RECURRING,
                "price": "85000.00",
                "recurring_interval": Interval.MONTH,
                "deposit_amount": "170000.00",
                "is_default": True,
            }
        ],
        "₹85,000 per month",
    ),
    (
        "Clinic — bookable service",
        {"name": "Dental Cleaning", "item_kind": Kind.SERVICE, "_bookable": True},
        [{"variant_label": "Standard", "price": "1500.00", "is_default": True}],
        "₹1,500",
    ),
    (
        "Gym / membership",
        {"name": "Annual Membership", "item_kind": Kind.SERVICE},
        [
            {
                "variant_label": "All-Access",
                "pricing_type": PricingType.RECURRING,
                "price": "12000.00",
                "recurring_interval": Interval.YEAR,
                "is_default": True,
            }
        ],
        "₹12,000 per year",
    ),
    (
        "Gym peak / off-peak (fixed rate buckets, not a rate calendar)",
        {"name": "Monthly Membership", "item_kind": Kind.SERVICE},
        [
            {
                "variant_label": "Off-Peak",
                "pricing_type": PricingType.RECURRING,
                "price": "800.00",
                "recurring_interval": Interval.MONTH,
                "is_default": True,
            },
            {
                "variant_label": "All-Access",
                "pricing_type": PricingType.RECURRING,
                "price": "1400.00",
                "recurring_interval": Interval.MONTH,
                "sort_order": 1,
            },
        ],
        "₹800 per month",
    ),
    (
        "Equipment rental",
        {"name": "JCB Excavator", "item_kind": Kind.PHYSICAL_GOOD},
        [
            {
                "variant_label": "With operator",
                "price": "2500.00",
                "unit_of_measure": Unit.HOUR,
                "deposit_amount": "25000.00",
                "min_order_quantity": "4.000",
                "is_default": True,
            }
        ],
        "₹2,500 per hour",
    ),
    (
        "Consulting / agency",
        {"name": "Website Development", "item_kind": Kind.SERVICE},
        [
            {
                "variant_label": "Standard build",
                "pricing_type": PricingType.FROM,
                "price": "50000.00",
                "is_default": True,
            }
        ],
        "from ₹50,000",
    ),
    (
        "Salon — with home visit",
        {
            "name": "Haircut",
            "item_kind": Kind.SERVICE,
            "fulfilment_modes": [Mode.AT_BUSINESS, Mode.AT_CUSTOMER],
        },
        [
            {
                "variant_label": "Adult",
                "price": "300.00",
                "duration_minutes": 30,
                "is_default": True,
            },
            {
                "variant_label": "Child",
                "price": "200.00",
                "duration_minutes": 20,
                "sort_order": 1,
            },
            {
                "variant_label": "Home visit",
                "price": "500.00",
                "duration_minutes": 45,
                "sort_order": 2,
            },
        ],
        "₹300",
    ),
    (
        "Education / coaching",
        {
            "name": "IELTS Crash Course",
            "item_kind": Kind.SERVICE,
            "attributes": {"batch_size": 15, "level": "B2", "weeks": 6},
        },
        [
            {
                "variant_label": "Weekday batch",
                "price": "25000.00",
                "unit_of_measure": Unit.SESSION,
                "unit_label": "for 12 sessions",
                "is_default": True,
            }
        ],
        "₹25,000 for 12 sessions",
    ),
    (
        "Digital goods",
        {"name": "Recipe eBook", "item_kind": Kind.DIGITAL_GOOD,
         "fulfilment_modes": [Mode.REMOTE]},
        [{"variant_label": "PDF", "price": "299.00", "is_default": True}],
        "₹299",
    ),
    (
        "Free lead-gen offer",
        {"name": "15-Minute Consultation", "item_kind": Kind.SERVICE},
        [
            {
                "variant_label": "Introductory call",
                "pricing_type": PricingType.FREE,
                "price": None,
                "is_default": True,
            }
        ],
        "Free",
    ),
    (
        "Wholesale / B2B",
        {"name": "OPC Cement", "item_kind": Kind.PHYSICAL_GOOD},
        [
            {
                "variant_label": "50 kg bag",
                "price": "380.00",
                "unit_of_measure": Unit.KG,
                "unit_label": "per bag",
                "min_order_quantity": "100.000",
                "quantity_increment": "10.000",
                "is_default": True,
            }
        ],
        "₹380 per bag",
    ),
    (
        "Seasonal listing",
        {
            "name": "Mango Shake",
            "item_kind": Kind.PHYSICAL_GOOD,
            "available_from": "2026-04-01",
            "available_until": "2026-06-30",
        },
        [
            {
                "variant_label": "Regular",
                "price": "120.00",
                "availability_status": Status.SEASONAL,
                "is_default": True,
            }
        ],
        "₹120",
    ),
    (
        "Insurance / loans — rate pricing",
        {
            "name": "Home Loan",
            "item_kind": Kind.SERVICE,
            "attributes": {"max_tenure_years": 30, "processing_fee_pct": 0.5},
        },
        [
            {
                "variant_label": "Salaried",
                "pricing_type": PricingType.RATE,
                "price": "8.40",
                "unit_of_measure": Unit.PERCENT,
                "unit_label": "per annum on the loan amount",
                "is_default": True,
            }
        ],
        "8.4% per annum on the loan amount",
    ),
    (
        "Taxi / courier — composite pricing",
        {
            "name": "Airport Drop",
            "item_kind": Kind.SERVICE,
            "fulfilment_modes": [Mode.AT_CUSTOMER],
        },
        [
            {
                "variant_label": "Sedan",
                "base_price": "50.00",
                "price": "18.00",
                "unit_of_measure": Unit.KM,
                "is_default": True,
            }
        ],
        "₹50 base + ₹18 per kilometre",
    ),
    (
        "Field repair — call-out charge",
        {
            "name": "AC Service Visit",
            "item_kind": Kind.SERVICE,
            "fulfilment_modes": [Mode.AT_CUSTOMER],
        },
        [
            {
                "variant_label": "Split AC",
                "base_price": "300.00",
                "price": "150.00",
                "unit_label": "per unit serviced",
                "is_default": True,
            }
        ],
        "₹300 base + ₹150 per unit serviced",
    ),
    (
        "Restaurant add-on",
        {"name": "Extra Cheese", "item_kind": Kind.ADD_ON},
        [{"variant_label": "Regular", "price": "40.00", "is_default": True}],
        "₹40",
    ),
    (
        "Retail attachment",
        {"name": "2-Year Extended Warranty", "item_kind": Kind.ADD_ON},
        [{"variant_label": "Standard", "price": "1999.00", "is_default": True}],
        "₹1,999",
    ),
    (
        "Tour operator — season buckets",
        {"name": "Goa 3N/4D", "item_kind": Kind.SERVICE},
        [
            {
                "variant_label": "Off season",
                "price": "12000.00",
                "unit_of_measure": Unit.PERSON,
                "is_default": True,
            },
            {
                "variant_label": "Peak season",
                "price": "18000.00",
                "unit_of_measure": Unit.PERSON,
                "sort_order": 1,
            },
        ],
        "₹12,000 per person",
    ),
    (
        "Pharmacy",
        {
            "name": "Dolo 650",
            "item_kind": Kind.PHYSICAL_GOOD,
            "brand": "Micro Labs",
            "attributes": {"prescription_required": False, "form": "tablet"},
            "pronunciation_hint": "DOH-lo six-fifty",
        },
        [
            {
                "variant_label": "Strip of 15",
                "price": "32.00",
                "unit_of_measure": Unit.PACK,
                "unit_label": "per strip of 15",
                "track_inventory": True,
                "stock_quantity": 60,
                "tax_code": "30049099",
                "is_default": True,
            }
        ],
        "₹32 per strip of 15",
    ),
    (
        "Quote-only project work",
        {"name": "Factory Shed Construction", "item_kind": Kind.SERVICE},
        [
            {
                "variant_label": "Turnkey",
                "pricing_type": PricingType.ON_REQUEST,
                "price": None,
                "is_default": True,
            }
        ],
        "Price on request",
    ),
    (
        "Diagnostics — price band",
        {"name": "MRI Scan", "item_kind": Kind.SERVICE},
        [
            {
                "variant_label": "Depending on region",
                "pricing_type": PricingType.RANGE,
                "price": "4000.00",
                "max_price": "9000.00",
                "is_default": True,
            }
        ],
        "₹4,000–₹9,000",
    ),
]


class BusinessTypeCoverageTests(CatalogTestCase):
    def _build(self, index, item_fields, variant_specs):
        item_fields = dict(item_fields)
        if item_fields.pop("_bookable", False):
            item_fields["event_template"] = EventTemplate.objects.create(
                tenant=self.tenant, name=f"Template {index}", duration_minutes=30
            )

        item = CatalogItem.objects.create(tenant=self.tenant, **item_fields)
        item.full_clean(exclude=AUDIT_FIELDS)

        variants = []
        for offset, spec in enumerate(variant_specs):
            spec = dict(spec)
            # Namespaced so one fixture's SKU cannot collide with another's — every
            # case in this list shares one tenant.
            if spec.get("sku"):
                spec["sku"] = f"{index}-{spec['sku']}"
            spec.setdefault("pricing_type", PricingType.FIXED)
            variant = CatalogVariant.objects.create(
                tenant=self.tenant, item=item, **spec
            )
            variant.full_clean(exclude=AUDIT_FIELDS)
            variants.append(variant)

        return item, variants

    def test_every_supported_business_type_can_be_listed(self):
        for index, (business, item_fields, variant_specs, expected_price) in enumerate(
            BUSINESS_CASES
        ):
            with self.subTest(business=business):
                item, variants = self._build(index, item_fields, variant_specs)

                self.assertEqual(item.variants.count(), len(variant_specs))

                default = item.default_variant
                self.assertIsNotNone(
                    default, f"{business}: no default variant to quote from"
                )
                self.assertEqual(default.display_price, expected_price)

    def test_the_matrix_covers_every_item_kind(self):
        """A kind with no fixture is a kind nobody has proved is usable."""
        covered = {fields["item_kind"] for _, fields, _, _ in BUSINESS_CASES}
        missing = set(Kind.values) - covered - {Kind.BUNDLE, Kind.OTHER}
        self.assertEqual(
            missing,
            set(),
            f"No business fixture exercises item_kind(s): {sorted(missing)}",
        )

    def test_the_matrix_covers_every_pricing_type(self):
        covered = set()
        for _, _, variant_specs, _ in BUSINESS_CASES:
            for spec in variant_specs:
                covered.add(spec.get("pricing_type", PricingType.FIXED))

        missing = set(PricingType.values) - covered
        self.assertEqual(
            missing,
            set(),
            f"No business fixture exercises pricing_type(s): {sorted(missing)}",
        )
