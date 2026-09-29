"""Forms behind the Manual onboarding screen in the Django admin.

Two steps, because every default on the second depends on the first: the plan and the
minutes decide the price, the credits and the allowances offered. Step one picks them;
step two shows everything pre-filled from them, and every value can be overridden,
exactly like the ``onboard_custom_deal`` command.

Every check a value needs happens here, before anything is written. Several go further
than the command does, because a form is the place to catch them:

* the phone number is normalised to E.164 and must fit the user's 15-character field
  (the command accepts up to 20 and fails at insert);
* the business name is capped so the subdomain derived from it fits its column;
* a payment reference already recorded on a successful transaction is refused, since
  it almost always means the same payment is being onboarded twice;
* a typed password must pass the configured password validators;
* a GSTIN must have the GSTIN shape.
"""

import hashlib
import json
import re
import secrets
from decimal import Decimal

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from onboard.constants.entitlement_resource import EntitlementResource
from onboard.constants.reset_period import ResetPeriod
from onboard.models import (
    Language,
    Plan,
    Timezone,
    Transaction,
    get_default_language,
    get_default_timezone,
)
from onboard.services.manual_onboarding import DEFAULT_STORAGE_GB, band_label
from user.models.CustomUser import CustomUser
from utils.phone import PhoneNormalizationError, normalize_phone

#: Largest value a PositiveIntegerField holds on Postgres. Credits and minutes land in
#: such columns, and a bigger number is a database error rather than a form error.
MAX_INT = 2_147_483_647

#: Longest business name accepted. The tenant's subdomain is derived from it, and
#: Tenant.subdomain is 100 characters with room needed for a "-N" collision suffix.
BUSINESS_NAME_MAX_LENGTH = 90

#: CustomUser.phone_number is 15 characters, and the number is stored as E.164.
PHONE_MAX_LENGTH = 15

#: 2-digit state code, 10-character PAN, entity number, "Z", check character.
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")

#: Length of a generated initial password, in URL-safe characters.
GENERATED_PASSWORD_BYTES = 12


def sellable_plans():
    """Plans a tenant can be put on here: not deleted, and sold by the minute.

    Activation refuses any other plan, so offering one would only move the failure
    from this list to the submit button.
    """
    plans = Plan.objects.filter(is_deleted=False).order_by("sort_order", "name")
    return [plan for plan in plans if plan.is_minutes_based]


def plan_label(plan):
    return (
        f"{plan.name} ({plan.code}) — {plan.billing_cycle}, {band_label(plan)} "
        f"at {plan.currency} {plan.basis_rate().price_per_minute_inr}/min"
    )


def entitlement_field_names(resource):
    """Form field names for one resource's allowance and its reset window."""
    return f"ent_{resource.value}", f"ent_{resource.value}_period"


class PlanStepForm(forms.Form):
    """Step one: the plan that supplies defaults and the rate card, and the minutes."""

    plan = forms.ModelChoiceField(
        queryset=Plan.objects.none(),
        empty_label=None,
        help_text=(
            "Supplies the defaults on the next page and the per-minute call rates. "
            "Only plans sold by the minute are listed; no other plan can be activated."
        ),
    )
    minutes = forms.IntegerField(
        min_value=0,
        max_value=MAX_INT,
        required=False,
        help_text=(
            "Minutes per cycle. Blank uses the plan's minimum. A negotiated quantity "
            "outside the plan's band is accepted."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        plans = sellable_plans()
        self.fields["plan"].queryset = Plan.objects.filter(
            pk__in=[plan.pk for plan in plans]
        ).order_by("sort_order", "name")
        self.fields["plan"].label_from_instance = plan_label

    def clean(self):
        cleaned = super().clean()
        plan = cleaned.get("plan")
        if plan is not None and cleaned.get("minutes") is None:
            cleaned["minutes"] = plan.min_minutes or plan.max_minutes or 0
        return cleaned


class ManualOnboardingForm(forms.Form):
    """Step two: the business, the payment, and the negotiated contract."""

    # --- 1. Business and primary user ------------------------------------------
    business_name = forms.CharField(max_length=BUSINESS_NAME_MAX_LENGTH)
    first_name = forms.CharField(max_length=100, label="Primary user first name")
    last_name = forms.CharField(
        max_length=100, required=False, label="Primary user last name"
    )
    email = forms.EmailField(
        max_length=254,
        label="Primary user email",
        help_text="Their sign-in. Must not already belong to any account.",
    )
    phone_number = forms.CharField(
        max_length=32,
        required=False,
        help_text="International format (+91...) or a local number; stored as E.164.",
    )
    password = forms.CharField(
        required=False,
        strip=False,
        label="Initial password",
        widget=forms.PasswordInput(render_value=True, attrs={"autocomplete": "new-password"}),
        help_text=(
            "Leave blank to generate one. A generated password is shown once, on the "
            "result page. They can reset it later."
        ),
    )
    referral_code = forms.CharField(max_length=50, required=False)
    timezone = forms.ModelChoiceField(
        queryset=Timezone.objects.all().order_by("timezone"), required=False
    )
    language = forms.ModelChoiceField(
        queryset=Language.objects.filter(is_active=True).order_by("name"),
        required=False,
    )

    # --- 2. Billing address ------------------------------------------------------
    record_address = forms.BooleanField(
        required=False, initial=True, label="Record a billing address"
    )
    line1 = forms.CharField(max_length=255, required=False, label="Address line 1")
    city = forms.CharField(max_length=100, required=False)
    state = forms.CharField(max_length=100, required=False)
    zip_code = forms.CharField(max_length=20, required=False, label="Postcode")
    country = forms.CharField(max_length=100, required=False, initial="India")
    gstin = forms.CharField(
        max_length=15, required=False, label="GSTIN", help_text="Optional. 15 characters."
    )

    # --- 3. Payment received -------------------------------------------------------
    amount = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=Decimal("0"),
        label="Amount received",
        help_text="What was actually paid. Defaults to the plan's price for these minutes.",
    )
    payment_reference = forms.CharField(
        max_length=255,
        help_text="Bank UTR, cheque number or invoice. Must not already be recorded.",
    )

    # --- 4. Negotiated terms -------------------------------------------------------
    credits = forms.IntegerField(
        min_value=0, max_value=MAX_INT, label="Credits to grant for this cycle"
    )
    max_rollover_credits = forms.IntegerField(min_value=0, max_value=MAX_INT)
    low_balance_threshold = forms.IntegerField(
        min_value=0, max_value=MAX_INT, label="Low balance alert threshold"
    )
    allow_rollover_on_renewal = forms.BooleanField(
        required=False, label="Roll over unused credits on renewal"
    )
    allow_rollover_on_upgrade = forms.BooleanField(
        required=False, label="Roll over on upgrade"
    )
    allow_rollover_on_downgrade = forms.BooleanField(
        required=False, label="Roll over on downgrade"
    )
    allow_use_after_expiry = forms.BooleanField(
        required=False, label="Allow credit use after the cycle expires"
    )
    renews_without_payment = forms.BooleanField(
        required=False, label="Renew automatically without a new payment"
    )

    # --- 6. Audit and confirmation ---------------------------------------------------
    reason = forms.CharField(
        min_length=10,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=(
            "Why this tenant is being onboarded by hand: the contract, PO or approval. "
            "Recorded in the operator audit log with your name."
        ),
    )
    otp_token = forms.CharField(
        required=False,
        max_length=8,
        label="Authenticator code",
        widget=forms.TextInput(attrs={
            "autocomplete": "one-time-code", "inputmode": "numeric", "size": 8,
        }),
        help_text="The current 6-digit code from your authenticator app.",
    )

    #: Fields shown under each heading, in order. Entitlements are added in __init__.
    SECTIONS = (
        ("Business and primary user", (
            "business_name", "first_name", "last_name", "email", "phone_number",
            "password", "referral_code", "timezone", "language",
        )),
        ("Billing address", (
            "record_address", "line1", "city", "state", "zip_code", "country", "gstin",
        )),
        ("Payment received", ("amount", "payment_reference")),
        ("Negotiated terms", (
            "credits", "max_rollover_credits", "low_balance_threshold",
            "allow_rollover_on_renewal", "allow_rollover_on_upgrade",
            "allow_rollover_on_downgrade", "allow_use_after_expiry",
            "renews_without_payment",
        )),
    )

    def __init__(self, *args, plan, minutes, **kwargs):
        super().__init__(*args, **kwargs)
        self.plan = plan
        self.minutes = minutes
        self.generated_password = False

        # The plan's figures, offered as defaults. Every one can be overwritten.
        defaults = {
            "amount": plan.price_components(minutes).total,
            "credits": plan.credits_for(minutes) or 0,
            "max_rollover_credits": plan.max_rollover_credits or 0,
            "low_balance_threshold": plan.low_balance_threshold or 0,
            "allow_rollover_on_renewal": plan.allow_rollover_on_renewal,
            "allow_rollover_on_upgrade": plan.allow_rollover_on_upgrade,
            "allow_rollover_on_downgrade": plan.allow_rollover_on_downgrade,
            "allow_use_after_expiry": plan.allow_use_after_expiry,
            "renews_without_payment": plan.renews_without_payment,
            "timezone": get_default_timezone(),
            "language": get_default_language(),
        }
        for name, value in defaults.items():
            self.fields[name].initial = value

        self._plan_entitlements = {row.resource: row for row in plan.entitlements.all()}
        self.entitlement_rows = []
        for resource in EntitlementResource:
            included_name, period_name = entitlement_field_names(resource)
            existing = self._plan_entitlements.get(resource.value)
            offered = (
                "unlimited"
                if existing is None or existing.included_quantity is None
                else f"{existing.included_quantity:,}"
            )
            is_storage = resource == EntitlementResource.STORAGE_GB
            self.fields[included_name] = forms.IntegerField(
                min_value=0,
                max_value=MAX_INT,
                required=False,
                label=resource.label,
                initial=DEFAULT_STORAGE_GB if is_storage else None,
                help_text=(
                    f"Plan offers {offered}. Blank means unlimited"
                    + (
                        f"; storage defaults to {DEFAULT_STORAGE_GB} GB because "
                        "unlimited storage is a bill nobody agreed to."
                        if is_storage else "."
                    )
                ),
            )
            self.fields[period_name] = forms.ChoiceField(
                choices=ResetPeriod.choices,
                label=f"{resource.label} resets",
                initial=existing.reset_period if existing else ResetPeriod.NONE,
                help_text="Only applies when an allowance is set.",
            )
            self.entitlement_rows.append((resource, included_name, period_name))

    # --- Field validation ------------------------------------------------------------

    def clean_business_name(self):
        return self.cleaned_data["business_name"].strip()

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        # Not filtered on is_deleted: `email` is unique at the database level and a
        # soft delete does not release it, so a soft-deleted row still collides.
        if CustomUser.objects.filter(email__iexact=email).exists():
            raise ValidationError("That email already has an account.")
        return email

    def clean_phone_number(self):
        raw = self.cleaned_data["phone_number"].strip()
        if not raw:
            return ""
        try:
            e164, _region = normalize_phone(raw)
        except PhoneNormalizationError as exc:
            raise ValidationError(str(exc))
        if len(e164) > PHONE_MAX_LENGTH:
            raise ValidationError(
                f"{e164} is longer than the {PHONE_MAX_LENGTH} characters a user's "
                "phone number can hold."
            )
        return e164

    def clean_password(self):
        password = self.cleaned_data["password"]
        if password:
            validate_password(password)
        return password

    def clean_gstin(self):
        gstin = self.cleaned_data["gstin"].strip().upper()
        if gstin and not GSTIN_RE.match(gstin):
            raise ValidationError(
                "Not a valid GSTIN: 15 characters -- 2-digit state code, 10-character "
                "PAN, entity number, 'Z', check character."
            )
        return gstin

    def clean_payment_reference(self):
        reference = self.cleaned_data["payment_reference"].strip()
        existing = Transaction.objects.filter(
            razorpay_payment_id__iexact=reference,
            status=Transaction.TransactionStatusChoices.SUCCESSFUL,
        ).first()
        if existing is not None:
            raise ValidationError(
                f"This reference is already recorded on order {existing.razorpay_order_id}. "
                "The same payment cannot onboard two tenants."
            )
        return reference

    def clean_reason(self):
        return self.cleaned_data["reason"].strip()

    def clean(self):
        cleaned = super().clean()

        if cleaned.get("record_address"):
            for name in ("line1", "city", "state", "zip_code", "country"):
                if not (cleaned.get(name) or "").strip():
                    self.add_error(name, "Required when recording a billing address.")

        if "password" in cleaned and not cleaned["password"]:
            cleaned["password"] = secrets.token_urlsafe(GENERATED_PASSWORD_BYTES)
            self.generated_password = True

        return cleaned

    # --- What the service and the audit log receive ----------------------------------

    def registrant(self):
        data = self.cleaned_data
        return {
            "business_name": data["business_name"],
            "first_name": data["first_name"].strip(),
            "last_name": data["last_name"].strip(),
            "email": data["email"],
            "phone_number": data["phone_number"],
            "password": data["password"],
            "referral_code": data["referral_code"].strip(),
            "timezone": data["timezone"],
            "language": data["language"],
        }

    def address(self):
        data = self.cleaned_data
        if not data["record_address"]:
            return None
        return {
            "line1": data["line1"].strip(),
            "city": data["city"].strip(),
            "state": data["state"].strip(),
            "zip_code": data["zip_code"].strip(),
            "country": data["country"].strip(),
            "gstin": data["gstin"],
        }

    def payment(self):
        return {
            "amount": self.cleaned_data["amount"],
            "reference": self.cleaned_data["payment_reference"],
        }

    def terms(self):
        data = self.cleaned_data
        return {
            name: data[name]
            for name in (
                "credits", "max_rollover_credits", "low_balance_threshold",
                "allow_rollover_on_renewal", "allow_rollover_on_upgrade",
                "allow_rollover_on_downgrade", "allow_use_after_expiry",
                "renews_without_payment",
            )
        }

    def entitlements(self):
        """``{resource: {"included": int | None, "reset_period": str}}``, as the
        command builds it. An unlimited allowance keeps the plan's window rather than
        the form's, because a window on a standing total quietly changes what the
        number means."""
        collected = {}
        for resource, included_name, period_name in self.entitlement_rows:
            included = self.cleaned_data[included_name]
            if included is None:
                existing = self._plan_entitlements.get(resource.value)
                period = existing.reset_period if existing else ResetPeriod.NONE
            else:
                period = self.cleaned_data[period_name]
            collected[resource.value] = {"included": included, "reset_period": period}
        return collected

    def audit_payload(self):
        """Everything that was requested, JSON-safe, **without the password**."""
        registrant = {
            key: value for key, value in self.registrant().items() if key != "password"
        }
        registrant["timezone"] = str(registrant["timezone"]) if registrant["timezone"] else None
        registrant["language"] = str(registrant["language"]) if registrant["language"] else None
        return {
            "registrant": registrant,
            "password_generated": self.generated_password,
            "address": self.address(),
            "plan": {"id": str(self.plan.pk), "code": self.plan.code},
            "minutes": self.minutes,
            "payment": {
                "amount": str(self.cleaned_data["amount"]),
                "reference": self.cleaned_data["payment_reference"],
            },
            "terms": self.terms(),
            "entitlements": self.entitlements(),
        }

    def review_digest(self):
        """A fingerprint of what the review showed.

        Sent back with Create and compared, so a value edited after the review cannot
        be created without the operator seeing it summarised first. A mismatch asks
        for another review; it is a guard against a slip, not against tampering, since
        the operator is the one submitting either way.
        """
        canonical = json.dumps(self.audit_payload(), sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()

    # --- Template helpers ---------------------------------------------------------------

    def bound_sections(self):
        """``[(heading, [BoundField, ...]), ...]`` in display order."""
        return [(heading, [self[name] for name in names]) for heading, names in self.SECTIONS]

    def bound_entitlements(self):
        """``[(included BoundField, period BoundField), ...]``, one per resource."""
        return [(self[included], self[period]) for _r, included, period in self.entitlement_rows]

    def review_sections(self):
        """``[(heading, [(label, value), ...]), ...]`` for the review panel."""
        registrant = self.registrant()
        address = self.address()
        payment = self.payment()
        terms = self.terms()

        def yes_no(value):
            return "Yes" if value else "No"

        user = f"{registrant['first_name']} {registrant['last_name']}".strip()
        sections = [
            ("Business and primary user", [
                ("Business", registrant["business_name"]),
                ("Primary user", f"{user} <{registrant['email']}>"),
                ("Phone", registrant["phone_number"] or "—"),
                ("Initial password",
                 "Generated; shown once after creation" if self.generated_password
                 else "As typed"),
                ("Timezone", registrant["timezone"] or "—"),
                ("Language", registrant["language"] or "—"),
            ]),
            ("Billing address", [
                ("Address",
                 f"{address['line1']}, {address['city']}, {address['state']} "
                 f"{address['zip_code']}, {address['country']}" if address else "Not recorded"),
                ("GSTIN", (address or {}).get("gstin") or "—"),
            ]),
            ("Plan and payment", [
                ("Plan (template)", f"{self.plan.name} ({self.plan.code})"),
                ("Minutes per cycle", f"{self.minutes:,}"),
                ("Amount paid", f"{self.plan.currency} {payment['amount']}"),
                ("Reference", payment["reference"]),
                ("Validity", f"{self.plan.billing_cycle} (from the plan)"),
            ]),
            ("Negotiated terms", [
                ("Credits granted", f"{terms['credits']:,}"),
                ("Rollover cap", f"{terms['max_rollover_credits']:,}"),
                ("Low balance alert at", f"{terms['low_balance_threshold']:,}"),
                ("Roll over on renewal", yes_no(terms["allow_rollover_on_renewal"])),
                ("Roll over on upgrade", yes_no(terms["allow_rollover_on_upgrade"])),
                ("Roll over on downgrade", yes_no(terms["allow_rollover_on_downgrade"])),
                ("Use after expiry", yes_no(terms["allow_use_after_expiry"])),
                ("Renews without payment", yes_no(terms["renews_without_payment"])),
            ]),
            ("Allowances", [
                (
                    EntitlementResource(resource).label,
                    "Unlimited" if entry["included"] is None
                    else f"{entry['included']:,} ({ResetPeriod(entry['reset_period']).label})",
                )
                for resource, entry in self.entitlements().items()
            ]),
        ]
        return sections
