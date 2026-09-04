"""Onboard a business that paid outside the payment gateway, on negotiated terms.

The normal signup path is Razorpay-shaped: a plan is picked from a list, an order is
created, the gateway verifies it, and the tenant gets exactly what that plan
advertises. An enterprise contract fits none of that. The money arrives by bank
transfer, and the terms — seats, credits, campaign volume — were agreed in a meeting,
not read off a pricing page.

This command is that path. It walks through the same sequence a real payment does, so
the result is an ordinary tenant with an ordinary subscription that every other part of
the system already understands, with two differences:

* the order is recorded as already paid and verified, because it was;
* the terms are typed in rather than copied from the plan.

**The plan is a starting point, never a constraint.** Every value it offers is shown as
a default and can be overwritten. What gets written is ``Subscription.plan_snapshot``,
which is the tenant's frozen contract — `included_entitlement` and friends read it
before they read the plan, so a custom limit is honoured everywhere with no special
cases. The ``Plan`` row itself is never touched, so the deal cannot leak into anybody
else's pricing.

**Call rates are the deliberate exception.** The per-minute rate card is stamped from
the selected plan, because that is what the wallet rates calls against. Pick a plan
whose rates match what was agreed.

    python manage.py onboard_custom_deal
    python manage.py onboard_custom_deal --dry-run
"""

import uuid
from decimal import Decimal, InvalidOperation

from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import transaction as db_transaction

from onboard.constants.entitlement_resource import EntitlementResource
from onboard.constants.reset_period import ResetPeriod
from onboard.models import BillingAddress, Plan, TempUser, Transaction
from onboard.models.language import Language
from onboard.models.timezone import Timezone

#: What storage falls back to when the operator leaves it blank. Every other
#: allowance falls back to unlimited; storage does not, because "unlimited" there
#: is an open-ended hosting bill rather than a generous seat count.
DEFAULT_STORAGE_GB = 10

class Command(BaseCommand):
    help = "Interactively onboard a business that paid offline, on negotiated terms."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Collect and show everything, then exit without writing.",
        )

    # ------------------------------------------------------------------
    # Prompt helpers
    # ------------------------------------------------------------------
    # Every one of these accepts free entry. Where a field is an enum the choices are
    # listed and numbered, but a typed value is still accepted — the brief was full
    # control, and a prompt that only offers a menu is a prompt that blocks the one
    # deal that does not fit it.

    def _in(self, prompt):
        try:
            return input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            raise CommandError("\nCancelled; nothing was written.")

    def _ask(self, label, *, default=None, required=True, validator=None):
        suffix = f" [{default}]" if default not in (None, "") else ""
        while True:
            raw = self._in(f"  {label}{suffix}: ")
            if not raw and default is not None:
                raw = str(default)
            if not raw:
                if not required:
                    return ""
                self.stderr.write("    Required.")
                continue
            if validator:
                try:
                    validator(raw)
                except Exception as exc:
                    self.stderr.write(f"    {exc}")
                    continue
            return raw

    def _ask_choice(self, label, choices, *, default_index=None):
        """`choices` is a list of (value, display). Returns the chosen value."""
        self.stdout.write(f"  {label}")
        for index, (_value, display) in enumerate(choices, start=1):
            marker = " (default)" if default_index == index else ""
            self.stdout.write(f"    {index}) {display}{marker}")

        while True:
            raw = self._in("    Select: ")
            if not raw and default_index:
                return choices[default_index - 1][0]
            if raw.isdigit() and 1 <= int(raw) <= len(choices):
                return choices[int(raw) - 1][0]
            # Typed value, not a menu number. Accepted so an option the menu does not
            # list is still reachable.
            for value, _display in choices:
                if raw == str(value):
                    return value
            self.stderr.write("    Not a listed option.")

    def _ask_int(self, label, *, default=None, allow_unlimited=False, minimum=0):
        # Blank means unlimited only where there is nothing to fall back to.
        # Saying so next to a real default would be a straight contradiction.
        if allow_unlimited and default is None:
            hint = " (blank = unlimited)"
        elif allow_unlimited:
            hint = " (type 'unlimited' for no cap)"
        else:
            hint = ""
        shown = "unlimited" if default is None and allow_unlimited else default
        suffix = f" [{shown}]" if shown is not None else ""
        while True:
            raw = self._in(f"  {label}{hint}{suffix}: ")
            if not raw:
                if allow_unlimited and default is None:
                    return None
                if default is not None:
                    return default
                self.stderr.write("    Required.")
                continue
            if allow_unlimited and raw.lower() in {"unlimited", "none", "-"}:
                return None
            try:
                value = int(raw)
            except ValueError:
                self.stderr.write("    Whole number, please.")
                continue
            if value < minimum:
                self.stderr.write(f"    Must be at least {minimum}.")
                continue
            return value

    def _ask_decimal(self, label, *, default=None):
        suffix = f" [{default}]" if default is not None else ""
        while True:
            raw = self._in(f"  {label}{suffix}: ")
            if not raw and default is not None:
                return Decimal(str(default))
            try:
                value = Decimal(raw)
            except (InvalidOperation, TypeError):
                self.stderr.write("    Not a number.")
                continue
            if value < 0:
                self.stderr.write("    Cannot be negative.")
                continue
            return value

    def _ask_bool(self, label, *, default):
        suffix = "[Y/n]" if default else "[y/N]"
        while True:
            raw = self._in(f"  {label} {suffix}: ").lower()
            if not raw:
                return default
            if raw in {"y", "yes"}:
                return True
            if raw in {"n", "no"}:
                return False
            self.stderr.write("    y or n.")

    def _heading(self, text):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(text))
        self.stdout.write("-" * len(text))

    # ------------------------------------------------------------------

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        self.stdout.write(
            self.style.MIGRATE_HEADING("\nManual enterprise onboarding")
        )
        self.stdout.write(
            "Records an offline payment and provisions a tenant on negotiated terms.\n"
            "The selected plan supplies defaults and the call rate card; every other\n"
            "value here overrides it, and the plan itself is never modified.\n"
        )

        registrant = self._collect_registrant()
        address = self._collect_address()
        plan = self._select_plan()
        payment = self._collect_payment(plan)
        terms = self._collect_terms(plan)
        entitlements = self._collect_entitlements(plan)

        self._review(registrant, address, plan, payment, terms, entitlements)

        if dry_run:
            self.stdout.write(
                self.style.WARNING("\nDry run — nothing was written.")
            )
            return

        if not self._ask_bool("\nCreate all of the above?", default=False):
            self.stdout.write("Cancelled; nothing was written.")
            return

        subscription = self._provision(
            registrant, address, plan, payment, terms, entitlements
        )
        self._report(subscription, registrant)

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------

    def _collect_registrant(self):
        self._heading("1. Business and primary user")

        def _email_taken(value):
            from user.models.CustomUser import CustomUser

            validate_email(value)
            # Not filtered on is_deleted: `email` is unique at the database level and
            # a soft delete does not release that, so a soft-deleted row would still
            # collide at insert.
            if CustomUser.objects.filter(email__iexact=value).exists():
                raise ValidationError("That email already has an account.")

        business_name = self._ask("Business name")
        first_name = self._ask("Primary user first name")
        last_name = self._ask("Primary user last name", required=False)
        email = self._ask("Primary user email", validator=_email_taken)
        phone_number = self._ask("Phone number", required=False)
        password = self._ask("Initial password (they can reset it later)")
        referral_code = self._ask("Referral code", required=False)

        timezones = list(Timezone.objects.all().order_by("timezone")[:500])
        languages = list(Language.objects.filter(is_active=True).order_by("name"))

        timezone_obj = None
        if timezones:
            timezone_obj = self._ask_choice(
                "Timezone",
                [(tz, f"{tz.timezone} ({tz.offset})") for tz in timezones],
                default_index=1,
            )
        language_obj = None
        if languages:
            language_obj = self._ask_choice(
                "Language",
                [(lang, f"{lang.name} ({lang.code})") for lang in languages],
                default_index=1,
            )

        return {
            "business_name": business_name,
            "first_name": first_name,
            "last_name": last_name,
            "email": email.lower(),
            "phone_number": phone_number,
            "password": password,
            "referral_code": referral_code,
            "timezone": timezone_obj,
            "language": language_obj,
        }

    def _collect_address(self):
        self._heading("2. Billing address")
        if not self._ask_bool("Record a billing address?", default=True):
            return None
        return {
            "line1": self._ask("Address line 1"),
            "city": self._ask("City"),
            "state": self._ask("State"),
            "zip_code": self._ask("Postcode"),
            "country": self._ask("Country", default="India"),
            "gstin": self._ask("GSTIN", required=False),
        }

    def _select_plan(self):
        self._heading("3. Plan (template and rate card)")
        plans = list(Plan.objects.filter(is_deleted=False).order_by("sort_order", "name"))
        if not plans:
            raise CommandError("No plans exist; create one before using this command.")

        self.stdout.write(
            "  The plan supplies the defaults below and the per-minute call rates.\n"
            "  Its allowances are only a starting point.\n"
        )
        return self._ask_choice(
            "Plan",
            [
                (p, f"{p.name} ({p.code}) — {p.currency} {p.total_price}, "
                    f"{p.billing_cycle}, {p.allocated_credits} credits")
                for p in plans
            ],
            default_index=1,
        )

    def _collect_payment(self, plan):
        self._heading("4. Payment received")
        self.stdout.write(
            "  Recorded as already paid and verified — the money arrived outside the\n"
            "  gateway, so there is nothing left to confirm.\n"
        )
        amount = self._ask_decimal("Amount received", default=plan.total_price)
        reference = self._ask(
            "Payment reference (bank UTR, cheque number, invoice)"
        )
        return {"amount": amount, "reference": reference}

    def _collect_terms(self, plan):
        self._heading("5. Negotiated terms")
        self.stdout.write("  Defaults are the plan's. Override anything.\n")

        credits = self._ask_int(
            "Credits to grant for this cycle",
            default=plan.allocated_credits or 0,
        )
        max_rollover = self._ask_int(
            "Max rollover credits", default=plan.max_rollover_credits or 0
        )
        low_balance = self._ask_int(
            "Low balance alert threshold", default=plan.low_balance_threshold or 0
        )
        return {
            "credits": credits,
            "max_rollover_credits": max_rollover,
            "low_balance_threshold": low_balance,
            "allow_rollover_on_renewal": self._ask_bool(
                "Roll over unused credits on renewal?",
                default=plan.allow_rollover_on_renewal,
            ),
            "allow_rollover_on_upgrade": self._ask_bool(
                "Roll over on upgrade?", default=plan.allow_rollover_on_upgrade
            ),
            "allow_rollover_on_downgrade": self._ask_bool(
                "Roll over on downgrade?", default=plan.allow_rollover_on_downgrade
            ),
            "allow_use_after_expiry": self._ask_bool(
                "Allow credit use after the cycle expires?",
                default=plan.allow_use_after_expiry,
            ),
            "renews_without_payment": self._ask_bool(
                "Renew automatically without a new payment?",
                default=plan.renews_without_payment,
            ),
        }

    def _collect_entitlements(self, plan):
        self._heading("6. Allowances")
        self.stdout.write(
            "  What this contract includes, per resource.\n"
            "  Blank means UNLIMITED — this is a negotiated contract, so an unstated\n"
            f"  allowance is an uncapped one. Storage is the exception and defaults to\n"
            f"  {DEFAULT_STORAGE_GB} GB, because unlimited storage is a bill nobody agreed to.\n"
            "  The plan's own figure is shown for reference; it is not applied unless\n"
            "  you type it.\n"
            "  These are written to the subscription, not to the plan.\n"
        )

        from_plan = {
            row.resource: row for row in plan.entitlements.all()
        }
        period_choices = [(p.value, p.label) for p in ResetPeriod]

        collected = {}
        for resource in EntitlementResource:
            existing = from_plan.get(resource.value)
            default_period = existing.reset_period if existing else ResetPeriod.NONE

            # Storage is the only resource with a default. Everywhere else `None`
            # means blank falls through to unlimited rather than to the plan's value.
            default_included = (
                DEFAULT_STORAGE_GB
                if resource == EntitlementResource.STORAGE_GB
                else None
            )
            plan_hint = (
                "unlimited"
                if existing is None or existing.included_quantity is None
                else str(existing.included_quantity)
            )

            self.stdout.write("")
            included = self._ask_int(
                f"{resource.label}  (plan offers {plan_hint})",
                default=default_included,
                allow_unlimited=True,
            )

            # The window only means something for a windowed allowance, and asking
            # for one on a standing total invites a value that quietly changes what
            # the number means.
            period = default_period
            if included is not None:
                period_index = next(
                    (
                        i
                        for i, (value, _l) in enumerate(period_choices, start=1)
                        if value == default_period
                    ),
                    1,
                )
                period = self._ask_choice(
                    "  …resets how often?",
                    period_choices,
                    default_index=period_index,
                )

            collected[resource.value] = {
                "included": included,
                "reset_period": period,
            }
        return collected

    # ------------------------------------------------------------------
    # Review
    # ------------------------------------------------------------------

    def _review(self, registrant, address, plan, payment, terms, entitlements):
        self._heading("Review")
        w = self.stdout.write

        w(f"  Business        {registrant['business_name']}")
        w(f"  Primary user    {registrant['first_name']} {registrant['last_name']} "
          f"<{registrant['email']}>")
        if address:
            w(f"  Billing address {address['line1']}, {address['city']}, "
              f"{address['state']} {address['zip_code']}, {address['country']}")
            if address["gstin"]:
                w(f"  GSTIN           {address['gstin']}")
        w("")
        w(f"  Plan (template) {plan.name} ({plan.code})")
        w(f"  Amount paid     {plan.currency} {payment['amount']}")
        w(f"  Reference       {payment['reference']}")
        w("")
        w(f"  Credits granted {terms['credits']}")
        w(f"  Validity        {plan.billing_cycle} (from the plan)")
        w(f"  Rollover cap    {terms['max_rollover_credits']}")
        w(f"  Low balance at  {terms['low_balance_threshold']}")
        w("")
        w("  Allowances:")
        for resource, entry in entitlements.items():
            included = entry["included"]
            shown = "unlimited" if included is None else str(included)
            period = "" if included is None else f"  ({entry['reset_period']})"
            w(f"    {resource:<18} {shown}{period}")

        w("")
        w(self.style.WARNING(
            "  Call rates come from the selected plan and are NOT customised here."
        ))

    # ------------------------------------------------------------------
    # Provisioning
    # ------------------------------------------------------------------

    @db_transaction.atomic
    def _provision(self, registrant, address, plan, payment, terms, entitlements):
        from onboard.services import subscriptions as subscription_service
        from onboard.views.onboarding.base import create_tenant_and_mapping
        from user.models.CustomUser import CustomUser

        # A real TempUser, not a stand-in. `create_tenant_and_mapping` reads it, and
        # going through the same object the paid path uses means this cannot drift
        # away from how a normal signup builds a tenant.
        temp_user = TempUser.objects.create(
            email=registrant["email"],
            phone_number=registrant["phone_number"],
            first_name=registrant["first_name"],
            last_name=registrant["last_name"],
            business_name=registrant["business_name"],
            referral_code=registrant["referral_code"],
            password_hash=make_password(registrant["password"]),
            selected_plan=plan,
            timezone=registrant["timezone"],
            language=registrant["language"],
            is_email_verified=True,
        )

        # The dummy order. `razorpay_order_id` is unique and non-null, so it needs a
        # value — prefixed `manual_` so it is obvious in support and in the ledger
        # that no gateway was involved. The reference the operator typed goes in the
        # payment id field, which is where a human looks for "what proves this".
        order_id = f"manual_{uuid.uuid4().hex[:20]}"
        txn = Transaction.objects.create(
            razorpay_order_id=order_id,
            razorpay_payment_id=payment["reference"],
            amount=payment["amount"],
            status=Transaction.TransactionStatusChoices.SUCCESSFUL,
            transaction_type=Transaction.TransactionTypeChoices.ONBOARDING,
            temp_user=temp_user,
            plan=plan,
        )

        user = CustomUser(
            email=registrant["email"],
            username=registrant["email"],
            phone_number=registrant["phone_number"],
            first_name=registrant["first_name"],
            last_name=registrant["last_name"],
            password=temp_user.password_hash,
            is_active=True,
            is_verified=True,
            timezone=registrant["timezone"],
            language=registrant["language"],
        )
        user.save()

        tenant = create_tenant_and_mapping(temp_user, user)

        subscription = subscription_service.activate(
            tenant,
            plan,
            transaction=txn,
            credit_grant=terms["credits"],
            notes=f"Manual enterprise onboarding — {payment['reference']}",
            snapshot_overrides={
                "entitlements": entitlements,
                "allocated_credits": terms["credits"],
                "max_rollover_credits": terms["max_rollover_credits"],
                "low_balance_threshold": terms["low_balance_threshold"],
                "allow_rollover_on_renewal": terms["allow_rollover_on_renewal"],
                "allow_rollover_on_upgrade": terms["allow_rollover_on_upgrade"],
                "allow_rollover_on_downgrade": terms["allow_rollover_on_downgrade"],
                "allow_use_after_expiry": terms["allow_use_after_expiry"],
                "renews_without_payment": terms["renews_without_payment"],
                # What was actually paid, which for a negotiated deal is not the
                # plan's list price. Frozen here so an invoice query does not have to
                # join back to the transaction.
                "total_price": str(payment["amount"]),
                "manual_payment_reference": payment["reference"],
            },
        )

        if address:
            BillingAddress.objects.create(user=user, **address)

        txn.user = user
        txn.tenant = tenant
        txn.subscription = subscription
        txn.temp_user = None
        txn.save(update_fields=[
            "user", "tenant", "subscription", "temp_user", "updated_at",
        ])

        temp_user.is_onboarded = True
        temp_user.save()
        temp_user.soft_delete()

        return subscription

    def _report(self, subscription, registrant):
        w = self.stdout.write
        w("")
        w(self.style.SUCCESS("Done."))
        w(f"  Tenant          {subscription.tenant.name} ({subscription.tenant_id})")
        w(f"  Subdomain       {subscription.tenant.subdomain}")
        w(f"  Subscription    {subscription.id}")
        w(f"  Cycle ends      {subscription.end_date:%d %b %Y}")
        w(f"  Sign in as      {registrant['email']}")
        w("")
        w("  The primary user is the tenant OWNER and can invite colleagues.")
        w("  Credits and allowances came from this contract, not the plan.")
