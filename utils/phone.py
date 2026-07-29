"""
E.164 phone normalization.

Contact uniqueness, import conflict detection, and inbound-caller lookup all key off
the normalized form. Matching raw strings would treat "+1 (555) 123-4567" and
"+15551234567" as two different people, which makes duplicate detection decorative.
"""

import phonenumbers
from django.conf import settings


class PhoneNormalizationError(ValueError):
    """Raised when a value cannot be resolved to a valid E.164 phone number."""


def is_supported_region(region):
    """True if `region` is an ISO-3166 alpha-2 code phonenumbers can parse against."""
    if not region:
        return False
    return str(region).strip().upper() in phonenumbers.SUPPORTED_REGIONS


def resolve_region(explicit=None):
    """
    The region used to parse numbers that are not already in international form.

    Order: explicit argument, then the deployment default. Returns None when neither
    is set — callers must treat that as an error rather than picking a country.
    """
    region = (explicit or "").strip().upper()
    if region:
        return region

    configured = (getattr(settings, "CONTACT_DEFAULT_PHONE_REGION", "") or "").strip().upper()
    return configured or None


def normalize_phone(raw, region=None):
    """
    Return `(e164, region_code)` for `raw`.

    Numbers written in international form ("+...") parse without a region. Anything
    else needs one, and a missing region is an error rather than a guess — guessing
    silently rewrites the number to the wrong country, which is worse than rejecting it.
    """
    if raw is None:
        raise PhoneNormalizationError("Phone number is required.")

    value = str(raw).strip()
    if not value:
        raise PhoneNormalizationError("Phone number is required.")

    is_international = value.startswith("+")
    resolved_region = resolve_region(region)

    if not is_international and not resolved_region:
        raise PhoneNormalizationError(
            "Phone number must be in international format (e.g. +14155552671), "
            "or a default region must be configured."
        )

    if not is_international and not is_supported_region(resolved_region):
        raise PhoneNormalizationError(
            f"'{resolved_region}' is not a recognised region code."
        )

    try:
        parsed = phonenumbers.parse(
            value, None if is_international else resolved_region
        )
    except phonenumbers.NumberParseException:
        raise PhoneNormalizationError(f"'{value}' is not a valid phone number.")

    if not phonenumbers.is_valid_number(parsed):
        raise PhoneNormalizationError(f"'{value}' is not a valid phone number.")

    e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    region_code = phonenumbers.region_code_for_number(parsed) or (resolved_region or "")

    return e164, region_code
