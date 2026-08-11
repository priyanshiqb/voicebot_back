"""
Shared validation for the catalog serializers.

Two things live here rather than being repeated four times.

**Tenant resolution.** Serializers need the caller's tenant to run per-tenant
uniqueness and cross-tenant ownership checks, and the tenant is not in the payload —
`TenantBaseViewSet.perform_create` injects it after validation. So it has to be read
back off the request.

**The pricing rules.** `CatalogVariant` carries ten check constraints. Left to the
database they surface as `IntegrityError`, which the global handler can only render as
a 500 — so every one of them is mirrored here as a field error. They are a module-level
function rather than serializer methods because two serializers need them: the
standalone variant endpoint and the nested `variants` array on item create. One
implementation, or they drift.
"""

import json

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from core.models.catalog_variant import CatalogVariant

PricingType = CatalogVariant.PricingType


def resolve_tenant_id(serializer):
    """The tenant this write belongs to, or ``None`` if it cannot be determined.

    Order matters: an existing instance's tenant wins over the request, so a
    superuser editing another tenant's row validates against *that* tenant rather
    than their own mapping.

    ``None`` means "cannot check" — callers must skip the check rather than treat it
    as a failure. The database constraints are the real guarantee; these checks exist
    to turn the ordinary case into a 400 on the right field.
    """
    instance = getattr(serializer, "instance", None)
    tenant_id = getattr(instance, "tenant_id", None)
    if tenant_id is not None:
        return tenant_id

    request = serializer.context.get("request")
    mapping = getattr(getattr(request, "user", None), "tenant_mapping", None)
    return getattr(mapping, "tenant_id", None)


def run_model_clean(instance, field_map=None):
    """Run ``instance.clean()`` and re-raise as a DRF error.

    The model's `clean()` is the single implementation of the cross-tenant and
    coherence rules, and DRF never calls it — `ModelSerializer.validate()` does not
    run `full_clean()`. Without this, every rule in `clean()` is enforced only in the
    admin and the shell, and the API happily writes rows the admin would refuse.

    `field_map` renames model field keys to serializer field names where they differ.
    """
    try:
        instance.clean()
    except DjangoValidationError as exc:
        detail = getattr(exc, "message_dict", None) or {"non_field_errors": exc.messages}
        if field_map:
            detail = {field_map.get(key, key): value for key, value in detail.items()}
        raise serializers.ValidationError(detail)


def validate_json_object(value, max_bytes, label="attributes"):
    """A JSON **object** (not a list or scalar), under a byte ceiling.

    The ceiling is the point: a JSONField with no bound is an unmetered write a tenant
    can perform on their own row, and the column is read on every catalog response.
    Measured as serialized bytes because that is what is stored and transferred —
    key count would let one enormous string through.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise serializers.ValidationError(
            f"{label} must be a JSON object, e.g. {{\"bhk\": 3}}."
        )

    size = len(json.dumps(value, separators=(",", ":")).encode("utf-8"))
    if size > max_bytes:
        raise serializers.ValidationError(
            f"{label} is {size} bytes; the maximum is {max_bytes}. "
            "Move long prose into `description`."
        )
    return value


def clean_string_array(value, max_items, max_length, label):
    """Strip, drop blanks, de-duplicate case-insensitively, and bound the result.

    De-duplication is case-insensitive but preserves the first spelling: a tenant who
    enters "Coke" and "coke" as aliases meant one alias, and keeping both doubles the
    work the agent's matcher does for no gain.
    """
    if value is None:
        return []
    if len(value) > max_items:
        raise serializers.ValidationError(
            f"At most {max_items} {label} are allowed; {len(value)} were given."
        )

    cleaned = []
    seen = set()
    for entry in value:
        entry = (entry or "").strip()
        if not entry:
            continue
        if len(entry) > max_length:
            raise serializers.ValidationError(
                f"Each {label[:-1] if label.endswith('s') else label} may be at most "
                f"{max_length} characters."
            )
        key = entry.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(entry)
    return cleaned


def _effective(attrs, instance, field, default=None):
    """The value a field will hold after this write.

    PATCH sends a subset, so a rule spanning two fields has to read the unchanged one
    off the instance. Reading only `attrs` is how a partial update slips past a
    coherence check — `pricing_type=RANGE` sent alone, with `max_price` already null.
    """
    if field in attrs:
        return attrs[field]
    if instance is not None:
        return getattr(instance, field, default)
    return default


def validate_variant_pricing(attrs, instance=None):
    """Mirror every `CatalogVariant` pricing constraint as a field error.

    Returns nothing; raises `serializers.ValidationError` on the first problem found
    per field. Deliberately exhaustive rather than clever: each rule below has a
    matching CHECK constraint in the database, and the pair is what makes a bad price
    a 400 for an API client and still impossible for a data fix.
    """
    pricing_type = _effective(attrs, instance, "pricing_type", PricingType.FIXED)
    price = _effective(attrs, instance, "price")
    base_price = _effective(attrs, instance, "base_price")
    max_price = _effective(attrs, instance, "max_price")
    compare_at = _effective(attrs, instance, "compare_at_price")
    deposit = _effective(attrs, instance, "deposit_amount")
    interval = _effective(attrs, instance, "recurring_interval")
    tax_rate = _effective(attrs, instance, "tax_rate_percentage")
    track_inventory = _effective(attrs, instance, "track_inventory", False)
    stock = _effective(attrs, instance, "stock_quantity")

    errors = {}

    for field, amount in (
        ("price", price),
        ("base_price", base_price),
        ("max_price", max_price),
        ("compare_at_price", compare_at),
        ("deposit_amount", deposit),
    ):
        if amount is not None and amount < 0:
            errors[field] = "Must not be negative."

    priced_types = {
        PricingType.FIXED,
        PricingType.FROM,
        PricingType.RECURRING,
        PricingType.RATE,
    }
    if pricing_type in priced_types and price is None:
        errors["price"] = (
            f"A price is required when pricing_type is {pricing_type}. Use ON_REQUEST "
            "if the figure is only given on enquiry."
        )

    if pricing_type == PricingType.RATE and price is not None:
        if price < 0 or price > 100:
            errors["price"] = (
                "A RATE is a percentage and must be between 0 and 100. "
                "8.4 means 8.4%, not ₹8.40."
            )

    if pricing_type == PricingType.RANGE:
        if max_price is None:
            errors["max_price"] = "A RANGE price needs an upper bound."
        if price is None:
            errors["price"] = "A RANGE price needs a lower bound."
    if max_price is not None and price is not None and max_price < price:
        errors["max_price"] = "The upper bound must not be below the lower bound."

    if pricing_type == PricingType.RECURRING:
        if interval is None:
            errors["recurring_interval"] = (
                "A RECURRING price needs an interval — DAY, WEEK, MONTH, QUARTER or "
                "YEAR."
            )
    elif interval is not None:
        errors["recurring_interval"] = (
            f"An interval only applies to RECURRING pricing, not {pricing_type}. "
            "A stale interval left on a fixed price makes the quoted figure ambiguous."
        )

    if tax_rate is not None and (tax_rate < 0 or tax_rate > 100):
        errors["tax_rate_percentage"] = "Must be between 0 and 100."

    if stock is not None:
        if stock < 0:
            errors["stock_quantity"] = "Must not be negative."
        elif not track_inventory:
            errors["stock_quantity"] = (
                "Stock is only meaningful when track_inventory is true. Set that "
                "first, or leave the quantity empty."
            )

    if errors:
        raise serializers.ValidationError(errors)


def normalize_currency(value):
    """Upper-case a currency code so `inr` is not a different currency from `INR`."""
    value = (value or "").strip().upper()
    if value and (len(value) != 3 or not value.isalpha()):
        raise serializers.ValidationError(
            "Currency must be a three-letter ISO-4217 code, e.g. INR or USD."
        )
    return value
