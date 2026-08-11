"""
The shared serializer helpers.

Tested directly rather than only through the four serializers that use them, for two
reasons. They are the single implementation of rules the whole module depends on, so a
regression here is a regression everywhere. And two of them have behaviour that is hard
to reach from the outside: `_effective`'s partial-update semantics, and
`resolve_tenant_id`'s precedence when an instance and a request disagree.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from rest_framework import serializers

from core.models.catalog_variant import CatalogVariant
from core.serializers.catalog_common import (
    clean_string_array,
    normalize_currency,
    resolve_tenant_id,
    run_model_clean,
    validate_json_object,
    validate_variant_pricing,
)
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping

User = get_user_model()
PricingType = CatalogVariant.PricingType


class _Stub(serializers.Serializer):
    """Minimal serializer so the helpers can be called with a real `context`."""


class ResolveTenantIdTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-helpers")
        self.other_tenant = Tenant.objects.create(name="Globex", subdomain="globex-helpers")
        self.user = User.objects.create_user(
            username="alex", email="alex@example.com", password="pw"
        )
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)

        request = RequestFactory().post("/")
        request.user = self.user
        self.request = request

    def test_it_reads_the_tenant_off_the_request(self):
        stub = _Stub(context={"request": self.request})
        self.assertEqual(resolve_tenant_id(stub), self.tenant.id)

    def test_an_existing_instance_wins_over_the_request(self):
        """So a superuser editing another tenant's row validates against *that*
        tenant, not their own mapping — otherwise every uniqueness check would be run
        against the wrong set of rows."""
        instance = CatalogVariant(tenant_id=self.other_tenant.id)
        stub = _Stub(instance=instance, context={"request": self.request})
        self.assertEqual(resolve_tenant_id(stub), self.other_tenant.id)

    def test_no_request_and_no_instance_resolves_to_none(self):
        """`None` means "cannot check", and callers must skip rather than fail. The
        database constraints are the real guarantee; these checks only exist to turn
        the ordinary case into a field error."""
        self.assertIsNone(resolve_tenant_id(_Stub(context={})))

    def test_a_user_with_no_tenant_mapping_resolves_to_none(self):
        unmapped = User.objects.create_user(
            username="nomap", email="nomap@example.com", password="pw"
        )
        request = RequestFactory().post("/")
        request.user = unmapped
        self.assertIsNone(resolve_tenant_id(_Stub(context={"request": request})))


class ValidateJsonObjectTests(TestCase):
    def test_none_becomes_an_empty_object(self):
        self.assertEqual(validate_json_object(None, 8192), {})

    def test_a_dict_passes_through(self):
        payload = {"bhk": 3, "amenities": ["lift"]}
        self.assertEqual(validate_json_object(payload, 8192), payload)

    def test_a_list_is_rejected(self):
        with self.assertRaises(serializers.ValidationError) as caught:
            validate_json_object([1, 2, 3], 8192)
        self.assertIn("JSON object", str(caught.exception))

    def test_a_scalar_is_rejected(self):
        with self.assertRaises(serializers.ValidationError):
            validate_json_object("just a string", 8192)

    def test_the_byte_ceiling_is_enforced(self):
        """Measured as serialized bytes, not key count: one enormous string would slip
        past a key-count limit, and it is the bytes that get stored and transferred on
        every catalog response."""
        with self.assertRaises(serializers.ValidationError) as caught:
            validate_json_object({"notes": "x" * 9000}, 8192)
        message = str(caught.exception)
        self.assertIn("bytes", message)
        self.assertIn("8192", message)

    def test_many_small_keys_are_fine_under_the_ceiling(self):
        payload = {f"key_{index}": index for index in range(100)}
        self.assertEqual(validate_json_object(payload, 8192), payload)

    def test_the_label_appears_in_the_message(self):
        with self.assertRaises(serializers.ValidationError) as caught:
            validate_json_object("nope", 8192, label="options")
        self.assertIn("options", str(caught.exception))


class CleanStringArrayTests(TestCase):
    def test_entries_are_stripped(self):
        self.assertEqual(
            clean_string_array(["  vip  ", "organic"], 25, 50, "tags"),
            ["vip", "organic"],
        )

    def test_blanks_are_dropped(self):
        self.assertEqual(
            clean_string_array(["vip", "", "   ", None], 25, 50, "tags"), ["vip"]
        )

    def test_duplicates_are_dropped_case_insensitively(self):
        """A tenant entering "Coke" and "coke" meant one alias. Keeping both doubles
        the work the agent's matcher does for nothing."""
        self.assertEqual(clean_string_array(["Coke", "coke", "COKE"], 25, 50, "aliases"), ["Coke"])

    def test_the_first_spelling_is_the_one_kept(self):
        self.assertEqual(clean_string_array(["Coke", "coke"], 25, 50, "aliases"), ["Coke"])

    def test_none_becomes_an_empty_list(self):
        self.assertEqual(clean_string_array(None, 25, 50, "tags"), [])

    def test_too_many_entries_is_rejected(self):
        with self.assertRaises(serializers.ValidationError) as caught:
            clean_string_array([f"tag-{i}" for i in range(26)], 25, 50, "tags")
        self.assertIn("At most 25 tags", str(caught.exception))

    def test_an_over_long_entry_is_rejected(self):
        with self.assertRaises(serializers.ValidationError) as caught:
            clean_string_array(["x" * 51], 25, 50, "tags")
        # The label is singularised for the per-entry message.
        self.assertIn("Each tag", str(caught.exception))

    def test_the_cap_counts_raw_entries_not_cleaned_ones(self):
        """Deliberate: the cap is a bound on the request body, and de-duplication
        happens after. 30 blank strings is still a 30-entry payload."""
        with self.assertRaises(serializers.ValidationError):
            clean_string_array(["vip"] * 26, 25, 50, "tags")


class NormalizeCurrencyTests(TestCase):
    def test_lowercase_is_upper_cased(self):
        self.assertEqual(normalize_currency("inr"), "INR")

    def test_whitespace_is_stripped(self):
        self.assertEqual(normalize_currency("  usd "), "USD")

    def test_blank_stays_blank(self):
        """So a field that is not being written does not fail validation."""
        self.assertEqual(normalize_currency(""), "")
        self.assertEqual(normalize_currency(None), "")

    def test_the_wrong_length_is_rejected(self):
        for value in ("IN", "INRR"):
            with self.subTest(value=value):
                with self.assertRaises(serializers.ValidationError):
                    normalize_currency(value)

    def test_digits_are_rejected(self):
        with self.assertRaises(serializers.ValidationError):
            normalize_currency("IN1")


class RunModelCleanTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-clean")

    def test_a_django_validation_error_becomes_a_drf_one(self):
        """DRF does not catch `django.core.exceptions.ValidationError`. Left
        untranslated, every rule on a model's `clean()` would escape the API as a 500
        rather than a 400."""
        from core.models.catalog_category import CatalogCategory

        parent = CatalogCategory.objects.create(
            tenant=Tenant.objects.create(name="Other", subdomain="other-clean"),
            name="Foreign",
        )
        probe = CatalogCategory(tenant_id=self.tenant.id, name="Child", parent=parent)

        with self.assertRaises(serializers.ValidationError) as caught:
            run_model_clean(probe)
        self.assertIn("parent", caught.exception.detail)

    def test_a_clean_instance_raises_nothing(self):
        from core.models.catalog_category import CatalogCategory

        run_model_clean(CatalogCategory(tenant_id=self.tenant.id, name="Menu"))

    def test_the_field_map_renames_keys(self):
        from core.models.catalog_category import CatalogCategory

        parent = CatalogCategory.objects.create(
            tenant=Tenant.objects.create(name="Other", subdomain="other-map"),
            name="Foreign",
        )
        probe = CatalogCategory(tenant_id=self.tenant.id, name="Child", parent=parent)

        with self.assertRaises(serializers.ValidationError) as caught:
            run_model_clean(probe, field_map={"parent": "parent_category"})
        self.assertIn("parent_category", caught.exception.detail)


class ValidateVariantPricingTests(TestCase):
    """The ten pricing rules, called directly.

    Also the only place `_effective`'s partial-update behaviour is reachable: a PATCH
    sends a subset, so a rule spanning two fields has to read the unchanged one off the
    instance. Reading only `attrs` is how `pricing_type=RANGE` sent on its own slips
    past the max-price rule.
    """

    def assert_error_on(self, field, attrs, instance=None):
        with self.assertRaises(serializers.ValidationError) as caught:
            validate_variant_pricing(attrs, instance=instance)
        self.assertIn(
            field,
            caught.exception.detail,
            f"expected an error on '{field}', got {caught.exception.detail}",
        )
        return caught.exception.detail

    # --- required price ---------------------------------------------------------

    def test_each_priced_type_requires_a_price(self):
        for pricing_type in (
            PricingType.FIXED,
            PricingType.FROM,
            PricingType.RECURRING,
            PricingType.RATE,
        ):
            with self.subTest(pricing_type=pricing_type):
                attrs = {"pricing_type": pricing_type, "price": None}
                if pricing_type == PricingType.RECURRING:
                    attrs["recurring_interval"] = CatalogVariant.RecurringInterval.MONTH
                self.assert_error_on("price", attrs)

    def test_on_request_and_free_need_no_price(self):
        validate_variant_pricing({"pricing_type": PricingType.ON_REQUEST, "price": None})
        validate_variant_pricing({"pricing_type": PricingType.FREE, "price": None})

    # --- non-negative money -----------------------------------------------------

    def test_every_money_field_rejects_a_negative(self):
        for field in (
            "price",
            "base_price",
            "max_price",
            "compare_at_price",
            "deposit_amount",
        ):
            with self.subTest(field=field):
                self.assert_error_on(
                    field,
                    {
                        "pricing_type": PricingType.FIXED,
                        "price": Decimal("10.00"),
                        field: Decimal("-1.00"),
                    },
                )

    # --- rate -------------------------------------------------------------------

    def test_a_rate_is_bounded_to_100(self):
        detail = self.assert_error_on(
            "price", {"pricing_type": PricingType.RATE, "price": Decimal("840.00")}
        )
        self.assertIn("percentage", str(detail["price"]))

    def test_a_rate_of_8_4_is_accepted(self):
        validate_variant_pricing(
            {"pricing_type": PricingType.RATE, "price": Decimal("8.40")}
        )

    def test_a_price_over_100_is_fine_when_it_is_not_a_rate(self):
        validate_variant_pricing(
            {"pricing_type": PricingType.FIXED, "price": Decimal("840.00")}
        )

    # --- range ------------------------------------------------------------------

    def test_a_range_needs_both_ends(self):
        self.assert_error_on(
            "max_price",
            {"pricing_type": PricingType.RANGE, "price": Decimal("2000.00")},
        )
        self.assert_error_on(
            "price",
            {"pricing_type": PricingType.RANGE, "max_price": Decimal("5000.00")},
        )

    def test_the_upper_bound_must_not_be_below_the_lower(self):
        self.assert_error_on(
            "max_price",
            {
                "pricing_type": PricingType.RANGE,
                "price": Decimal("5000.00"),
                "max_price": Decimal("2000.00"),
            },
        )

    # --- recurring --------------------------------------------------------------

    def test_recurring_needs_an_interval(self):
        self.assert_error_on(
            "recurring_interval",
            {"pricing_type": PricingType.RECURRING, "price": Decimal("12000.00")},
        )

    def test_a_non_recurring_row_may_not_carry_an_interval(self):
        """The other half of the rule. Without it a FIXED price can hold a stale MONTH
        interval left over from an edit, and the quoted figure becomes ambiguous."""
        detail = self.assert_error_on(
            "recurring_interval",
            {
                "pricing_type": PricingType.FIXED,
                "price": Decimal("100.00"),
                "recurring_interval": CatalogVariant.RecurringInterval.MONTH,
            },
        )
        self.assertIn("only applies to RECURRING", str(detail["recurring_interval"]))

    # --- tax and stock ----------------------------------------------------------

    def test_tax_rate_is_bounded(self):
        for rate in (Decimal("-1.00"), Decimal("101.00")):
            with self.subTest(rate=rate):
                self.assert_error_on(
                    "tax_rate_percentage",
                    {
                        "pricing_type": PricingType.FIXED,
                        "price": Decimal("10.00"),
                        "tax_rate_percentage": rate,
                    },
                )

    def test_stock_without_tracking_is_rejected(self):
        detail = self.assert_error_on(
            "stock_quantity",
            {
                "pricing_type": PricingType.FIXED,
                "price": Decimal("10.00"),
                "track_inventory": False,
                "stock_quantity": 5,
            },
        )
        self.assertIn("track_inventory", str(detail["stock_quantity"]))

    def test_negative_stock_is_rejected_before_the_tracking_rule(self):
        """Order matters for the message: "must not be negative" is more useful than
        "turn on tracking first" when the value is -1."""
        detail = self.assert_error_on(
            "stock_quantity",
            {
                "pricing_type": PricingType.FIXED,
                "price": Decimal("10.00"),
                "track_inventory": False,
                "stock_quantity": -1,
            },
        )
        self.assertIn("negative", str(detail["stock_quantity"]))

    def test_tracked_stock_is_accepted(self):
        validate_variant_pricing(
            {
                "pricing_type": PricingType.FIXED,
                "price": Decimal("10.00"),
                "track_inventory": True,
                "stock_quantity": 4,
            }
        )

    # --- partial updates --------------------------------------------------------

    def test_a_partial_update_reads_unchanged_fields_off_the_instance(self):
        """The subtle one. `pricing_type=RANGE` sent alone, on a row whose `max_price`
        is already null, must be rejected — reading only `attrs` would let it through
        and the database would then raise IntegrityError."""
        instance = CatalogVariant(
            pricing_type=PricingType.FIXED, price=Decimal("100.00"), max_price=None
        )
        self.assert_error_on(
            "max_price", {"pricing_type": PricingType.RANGE}, instance=instance
        )

    def test_a_partial_update_that_satisfies_the_rule_passes(self):
        instance = CatalogVariant(
            pricing_type=PricingType.FIXED, price=Decimal("100.00")
        )
        validate_variant_pricing(
            {"pricing_type": PricingType.RANGE, "max_price": Decimal("500.00")},
            instance=instance,
        )

    def test_clearing_a_price_on_a_fixed_row_is_rejected(self):
        instance = CatalogVariant(
            pricing_type=PricingType.FIXED, price=Decimal("100.00")
        )
        self.assert_error_on("price", {"price": None}, instance=instance)

    def test_switching_to_on_request_may_clear_the_price(self):
        instance = CatalogVariant(
            pricing_type=PricingType.FIXED, price=Decimal("100.00")
        )
        validate_variant_pricing(
            {"pricing_type": PricingType.ON_REQUEST, "price": None}, instance=instance
        )

    def test_several_broken_rules_are_reported_together(self):
        """One round trip per fix is a bad form experience. The validator collects."""
        with self.assertRaises(serializers.ValidationError) as caught:
            validate_variant_pricing(
                {
                    "pricing_type": PricingType.RANGE,
                    "price": Decimal("-5.00"),
                    "tax_rate_percentage": Decimal("120.00"),
                }
            )
        detail = caught.exception.detail
        self.assertIn("price", detail)
        self.assertIn("max_price", detail)
        self.assertIn("tax_rate_percentage", detail)
