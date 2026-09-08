"""Shared setup for the billing, credits and entitlements suites.

Roughly 700 test cases share the same world: a tenant with a user and an
authenticated client, a plan with a full rate card and a full set of entitlements,
and a subscription joining them. Rebuilding that inline in each test is how a suite
ends up asserting on its own setup rather than on behaviour, so it is built once here.

The helpers deliberately go through the **real** services — ``activate()``, the
webhook view, ``wallet.grant()`` — rather than writing rows directly. A factory that
shortcuts the code under test can make a broken system look healthy.
"""

import itertools
import uuid
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Sum
from django.utils import timezone
from rest_framework.test import APIClient

from onboard.constants.entitlement_resource import EntitlementResource
from onboard.constants.reset_period import ResetPeriod
from onboard.models import Plan, PlanEntitlement
from onboard.services import subscriptions as subscription_service
from tenant.models import Tenant, TenantUserMapping
from wallet.models import CreditLedger, CreditRate, Wallet

User = get_user_model()

#: Literal paths rather than ``reverse()``: these URLs are part of the contract the
#: tests exist to protect, so a rename should break them loudly here too.
WEBHOOK_URL = "/api/v1/onboard/webhook/"
BILLING = "/api/v1/billing"

Operation = CreditRate.Operation
Resource = EntitlementResource


def _suffix():
    return uuid.uuid4().hex[:8]


#: Contacts are unique per tenant on their E.164 number and the model validates it,
#: so a hex suffix will not do — these have to be real, distinct Indian mobiles.
_phone_numbers = itertools.count(1)


def _phone():
    return f"+9199{next(_phone_numbers):08d}"


# --------------------------------------------------------------------------- #
# Tenants
# --------------------------------------------------------------------------- #

class TenantContext:
    """A tenant, a user who belongs to it, and a client authenticated as them."""

    __slots__ = ("tenant", "user", "client")

    def __init__(self, tenant, user, client):
        self.tenant = tenant
        self.user = user
        self.client = client

    @property
    def id(self):
        return self.tenant.id

    @property
    def wallet(self):
        """Always re-read. A cached wallet in a test is a stale balance assertion."""
        return Wallet.objects.get(tenant=self.tenant)

    def __str__(self):
        return f"TenantContext({self.tenant.name})"


def make_tenant(name="acme", *, is_active=True):
    """A tenant with one user and an authenticated ``APIClient``.

    Subdomains and emails are suffixed because these models are unique on them and
    tests run in an unpredictable order.
    """
    suffix = _suffix()
    tenant = Tenant.objects.create(name=name, subdomain=f"{name}-{suffix}")
    user = User.objects.create(
        email=f"{name}-{suffix}@example.com",
        username=f"{name}-{suffix}",
        is_active=is_active,
    )
    TenantUserMapping.objects.create(user=user, tenant=tenant)

    client = APIClient()
    client.force_authenticate(user=user)
    return TenantContext(tenant, user, client)


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #

#: A complete rate card. Every plan gets all three tiers, priced at 0 where the plan
#: does not sell them — a *missing* row is what produces ``RateNotConfigured`` at call
#: time, and no plan should ever ship with a hole in its card.
DEFAULT_RATES = {
    Operation.STATIC_REEL: 4,
    Operation.CONVERSATION_HIGH_LATENCY: 8,
    Operation.CONVERSATION_LOW_LATENCY: 12,
}

#: A complete entitlement set, as ``(included, price_per_additional, step, max_extra)``.
#: Numbers are small so a test can exhaust a limit in a couple of lines rather than
#: looping up to a realistic production figure.
DEFAULT_ENTITLEMENTS = {
    Resource.AGENT: (2, Decimal("500.00"), 1, 10),
    Resource.USER: (3, Decimal("200.00"), 1, 20),
    Resource.CONTACT: (100, Decimal("100.00"), 100, 1000),
    Resource.KNOWLEDGE_BASE: (2, Decimal("300.00"), 1, 10),
    Resource.CRAWL_PAGE: (50, Decimal("50.00"), 50, 500),
    Resource.CAMPAIGN_MONTHLY: (5, Decimal("150.00"), 5, 50),
    Resource.STORAGE_GB: (5, Decimal("100.00"), 5, 100),
}


#: Rupees per minute beside ``DEFAULT_RATES``' credits per minute. Every tier works
#: out to ₹0.225 per credit, so a plan built from these is internally consistent
#: whichever tier it is sold at — the shape a real plan should have.
DEFAULT_MINUTE_PRICES = {
    Operation.STATIC_REEL: Decimal("0.90"),
    Operation.CONVERSATION_HIGH_LATENCY: Decimal("1.80"),
    Operation.CONVERSATION_LOW_LATENCY: Decimal("2.70"),
}


def make_plan(code=None, **overrides):
    """A plan with a full rate card and a full entitlement set.

    ``rates`` and ``entitlements`` overrides are merged into the defaults rather than
    replacing them, so a test that cares about one number does not silently drop the
    other twelve rows. Pass ``with_rates=False`` to build the misconfigured plan that
    ``RateNotConfigured`` tests need.
    """
    supplied_rates = overrides.pop("rates", None) or {}
    rates = {**DEFAULT_RATES, **supplied_rates}

    # --- the legacy shape, expressed in the model that replaced it ------------
    #
    # `base_price` and `allocated_credits` are gone from `Plan`, but several hundred
    # existing tests say "a plan costing X that grants N" and mean exactly that. The
    # new model can still say it: **a plan that sells exactly one minute**, priced at
    # X per minute and granting N credits per minute. A subscription to one minute of
    # it costs X and grants N, which is what those tests were asserting all along.
    #
    # So the translation is exact rather than approximate, and no test had to be
    # rewritten to keep meaning what it meant.
    legacy_price = overrides.pop("base_price", Decimal("1000.00"))
    legacy_credits = overrides.pop("allocated_credits", 1000)

    # Every plan is sold by the minute now, so the default factory plan is too --
    # otherwise most of the suite would be building plans that cannot be activated.
    # `make_plan(credit_basis=None)` still builds the unsellable one, for the tests
    # that need to prove it is refused.
    basis = overrides.get("credit_basis", Operation.CONVERSATION_HIGH_LATENCY)
    overrides["credit_basis"] = basis

    if basis is not None:
        rates = {
            op: spec if isinstance(spec, (tuple, list))
            else (spec, DEFAULT_MINUTE_PRICES[op])
            for op, spec in rates.items()
        }

        if not isinstance(supplied_rates.get(basis), (tuple, list)):
            # **Pack size is the grant divided by the call rate.**
            #
            # A test saying "a minute costs 5 credits and the cycle grants 1,000"
            # is describing a 200-minute plan, and both numbers survive: the call
            # rate stays 5, and 200 x 5 is the 1,000 it expects. The rupee price is
            # spread over the same pack, so a cycle still costs `base_price`.
            #
            # Callers that fix both numbers always choose ones that divide -- 1000/5,
            # 3000/3 -- because they were describing a coherent plan. Where they do
            # not, the grant is what survives, since far more assertions rest on it.
            per_minute = int(rates[basis][0])
            if legacy_credits and per_minute and int(legacy_credits) % per_minute == 0:
                # The happy path, and the one callers almost always land on: they
                # chose numbers that describe a coherent plan -- 1000/5, 3000/3.
                pack = int(legacy_credits) // per_minute
            elif legacy_credits:
                # **The grant wins over the rate when they cannot both be exact.**
                # Far more assertions rest on a balance than on a per-minute price,
                # and an approximate grant fails silently while a wrong balance fails
                # loudly. A test that needs both exact picks divisible numbers.
                pack, per_minute = 1, int(legacy_credits)
            else:
                # **A plan granting nothing per cycle is no longer expressible.** The
                # grant is minutes x credits-per-minute, and both are positive, so the
                # smallest a plan can give is one minute's worth. The call rate is left
                # alone -- zeroing it would make every call free, which is a different
                # product entirely. A caller that genuinely wants a zero grant says so
                # at activation, with `credit_grant=0`.
                pack = 1

            price = (
                Decimal(str(legacy_price)) / pack
            ).quantize(Decimal("0.0001")) if pack else Decimal("0.0000")
            rates[basis] = (per_minute, price)

            # The other two tiers priced at the same rupees-per-credit, so a factory
            # plan is internally consistent and produces no readiness warnings. A
            # plan that sells credits at three different prices is a legitimate thing
            # to build, but it should never be what a test gets by accident.
            per_credit = (price / per_minute) if per_minute else Decimal("0")
            for op, (credits_per_min, _default_price) in list(rates.items()):
                if op == basis:
                    continue
                rates[op] = (
                    credits_per_min,
                    (per_credit * credits_per_min).quantize(Decimal("0.0001")),
                )
            overrides.setdefault("min_minutes", pack)
            overrides.setdefault("max_minutes", pack)
        else:
            overrides.setdefault("min_minutes", 1)
            overrides.setdefault("max_minutes", 1)

    entitlements = {**DEFAULT_ENTITLEMENTS, **(overrides.pop("entitlements", None) or {})}
    with_rates = overrides.pop("with_rates", True)
    with_entitlements = overrides.pop("with_entitlements", True)

    suffix = _suffix()
    fields = {
        # Both are unique columns on Plan, so both are suffixed. A shared default
        # name is the kind of setup collision that fails ten unrelated tests at once.
        "code": code or f"plan_{suffix}",
        "name": f'{overrides.pop("name", "Test Plan")} {suffix}',
        "description": "Created by billing_factories",
        "discount_percentage": Decimal("0.00"),
        "tax_percentage": Decimal("18.00"),
        "currency": Plan.CurrencyChoices.INR,
        "billing_cycle": Plan.BillingCycleChoices.MONTHLY,
        "plan_type": Plan.PlanTypeChoices.PAID,
        "sort_order": 1,
        # A factory plan stands for a live one, so it is ACTIVE. `is_active` is
        # derived from this now -- passing it directly would be overwritten, which is
        # what stops the two disagreeing.
        "status": Plan.Status.ACTIVE,
        "cost_per_additional_credit": Decimal("1.00"),
        "max_rollover_credits": 0,
        "allow_rollover_on_renewal": True,
        "allow_rollover_on_upgrade": True,
        "allow_rollover_on_downgrade": False,
        "allow_use_after_expiry": False,
        "low_balance_threshold": 50,
        "renews_without_payment": False,
    }
    # `is_active=False` used to be how a test built a plan nobody can subscribe to.
    # It is derived now, so translate it rather than silently ignoring it.
    if overrides.pop("is_active", True) is False:
        overrides.setdefault("status", Plan.Status.RETIRED)

    fields.update(overrides)
    plan = Plan.objects.create(**fields)

    if with_rates:
        add_rates(plan, rates)
    if with_entitlements:
        add_entitlements(plan, entitlements)
    return plan


#: Trial credits a free plan grants. Its band is exactly this many minutes at one
#: credit each, so the number is both the minutes sold and the credits granted.
FREE_PLAN_CREDITS = 100


def make_free_plan(code=None, **overrides):
    """The shape a free plan has to have: costs nothing, renews itself, still meters.

    **Free in rupees, not free in credits.** The rate card is priced at ₹0.00 a minute
    so nothing is ever charged, but a call still costs a credit -- which is what makes
    the trial finite. A card priced at zero *credits* would be an unlimited trial, and
    a card with rows *missing* would raise on the tenant's first call.

    Capped at a maximum, because a plan that is free per minute and open at the top
    would grant unbounded credits for nothing. The activation gate refuses that.
    """
    defaults = {
        "name": "Free Plan",
        # Zero per minute, so whatever band the grant implies, a cycle costs nothing.
        # The band itself is left to `make_plan`, which derives it from the grant and
        # the call rate -- hardcoding one here would fight that arithmetic and produce
        # a plan granting something other than what the caller asked for.
        "base_price": Decimal("0.00"),
        "allocated_credits": FREE_PLAN_CREDITS,
        "tax_percentage": Decimal("0.00"),
        "plan_type": Plan.PlanTypeChoices.FREE,
        "cost_per_additional_credit": Decimal("0.00"),
        "renews_without_payment": True,
        "rates": {op: 1 for op in DEFAULT_RATES},
        "entitlements": {
            Resource.AGENT: (1, Decimal("0.00"), 1, 0),
            Resource.USER: (1, Decimal("0.00"), 1, 0),
        },
    }
    defaults.update(overrides)
    return make_plan(code or f"free_{_suffix()}", **defaults)


def add_rates(plan, rates, *, effective_from=None, unit=None):
    """Open-ended rate rows for an ``{operation: spec}`` mapping.

    ``spec`` is either ``credits_per_unit`` on its own, or
    ``(credits_per_unit, price_per_minute_inr)`` for a plan that is sold by the
    minute. The bare form leaves the rupee price null, which is what every legacy
    plan looks like.
    """
    effective_from = effective_from or timezone.now() - timedelta(days=1)
    created = {}
    for operation, spec in rates.items():
        if isinstance(spec, (tuple, list)):
            credits_per_unit, price_per_minute_inr = spec
        else:
            credits_per_unit, price_per_minute_inr = spec, None

        created[operation] = CreditRate.objects.create(
            plan=plan,
            operation=operation,
            unit=unit or CreditRate.Unit.PER_MINUTE,
            credits_per_unit=credits_per_unit,
            price_per_minute_inr=price_per_minute_inr,
            rounding=CreditRate.Rounding.CEIL,
            minimum_units=1,
            effective_from=effective_from,
        )
    return created


def make_minutes_plan(code=None, *, min_minutes=None, max_minutes=None,
                      credit_basis=Operation.CONVERSATION_HIGH_LATENCY,
                      minute_prices=None, **overrides):
    """A plan sold by the minute: a band, a basis, and rupee prices on every tier.

    Priced at ₹1.80/min and 8 credits/min on the default basis, so 1,000 minutes is
    ₹1,800 net and 8,000 credits.
    """
    prices = {**DEFAULT_MINUTE_PRICES, **(minute_prices or {})}
    credits = {**DEFAULT_RATES, **(overrides.pop("rates", None) or {})}

    overrides.setdefault("name", "Minutes Plan")
    return make_plan(
        code or f"minutes_{_suffix()}",
        min_minutes=min_minutes,
        max_minutes=max_minutes,
        credit_basis=credit_basis,
        rates={op: (credits[op], prices[op]) for op in credits},
        **overrides,
    )


def add_entitlements(plan, entitlements):
    """Entitlement rows for ``{resource: (included, price, step, max_additional)}``.

    Campaigns get a monthly window and everything else a standing total, matching
    what the real catalogue carries. A campaign allowance left as a standing total
    would count every campaign the tenant had ever created, which is a coherent
    setting but not the one any plan actually ships with.
    """
    created = {}
    for resource, spec in entitlements.items():
        included, price, step, max_additional = spec
        created[resource] = PlanEntitlement.objects.create(
            plan=plan,
            resource=resource,
            included_quantity=included,
            is_addon_available=bool(max_additional),
            price_per_additional=price,
            addon_step=step,
            max_additional=max_additional or None,
            reset_period=(
                ResetPeriod.MONTHLY
                if resource == Resource.CAMPAIGN_MONTHLY
                else ResetPeriod.NONE
            ),
        )
    return created


# --------------------------------------------------------------------------- #
# Subscriptions
# --------------------------------------------------------------------------- #

def subscribe(tenant, plan, **kwargs):
    """Put a tenant on a plan through the real activation path.

    Accepts a ``TenantContext`` or a ``Tenant``, because half the callers have one and
    half have the other.

    A quantity is filled in when the caller does not name one -- the plan's smallest
    sellable amount. Without it a plan sold by the minute would grant nothing, and
    every caller that predates minutes says only "put this tenant on this plan".
    """
    kwargs.setdefault("minutes", plan.min_minutes or plan.max_minutes)
    return subscription_service.activate(_tenant_of(tenant), plan, **kwargs)


def age_subscription(subscription, days_in):
    """Move the cycle window so ``days_in`` days of it have already elapsed.

    Proration is meaningless on a subscription that started this millisecond, and
    every pricing test needs a cycle with a middle. Shifts both ends so the cycle
    keeps its real length instead of being silently shortened.
    """
    shift = timedelta(days=days_in)
    type(subscription).objects.filter(pk=subscription.pk).update(
        start_date=subscription.start_date - shift,
        end_date=subscription.end_date - shift,
    )
    subscription.refresh_from_db()
    return subscription


def expire_subscription(subscription):
    """Age a subscription past its end date and run the real expiry path."""
    type(subscription).objects.filter(pk=subscription.pk).update(
        end_date=timezone.now() - timedelta(days=1),
    )
    subscription.refresh_from_db()
    subscription_service.expire(subscription)
    subscription.refresh_from_db()
    return subscription


# --------------------------------------------------------------------------- #
# Calls
# --------------------------------------------------------------------------- #

def make_agent(tenant, **overrides):
    """A minimal bot. ``service_tier`` is what billing actually reads off it."""
    from core.models.agent import Agent

    fields = {
        "tenant": _tenant_of(tenant),
        "bot_name": f"bot-{_suffix()}",
        "persona_name": f"persona-{_suffix()}",
    }
    fields.update(overrides)
    return Agent.objects.create(**fields)


def make_call_log(tenant, agent=None, **overrides):
    """A completed call, ready to be charged for.

    ``provider_call_id`` is the idempotency key the whole consumption path leans on,
    so it is unique per call unless a test deliberately reuses one.
    """
    from core.models.agent_call_log import AgentCallLog

    tenant = _tenant_of(tenant)
    fields = {
        "tenant": tenant,
        "agent": agent or make_agent(tenant),
        "provider_call_id": f"call_{_suffix()}{_suffix()[:4]}",
        "caller_number": "+919876543210",
        "direction": AgentCallLog.CallDirection.OUTBOUND,
        "duration_seconds": 90,
    }
    fields.update(overrides)
    return AgentCallLog.objects.create(**fields)


# --------------------------------------------------------------------------- #
# Countable resources
#
# Entitlements are gauges, counted live from the rows that *are* the resource.
# These build the real rows rather than a counter, because "delete it and the
# allowance comes back" is the property the entitlement tests exist to prove.
# --------------------------------------------------------------------------- #

def make_contact(tenant, phone=None, **overrides):
    from core.models.contact import Contact

    tenant = _tenant_of(tenant)
    fields = {
        "tenant": tenant,
        "first_name": "Test",
        "last_name": _suffix(),
        # A distinct, valid Indian number per contact: the tenant + E.164 pair is
        # unique, and a repeat would fail as a duplicate rather than as a quota.
        "phone_number": phone or _phone(),
        "phone_region": "IN",
    }
    fields.update(overrides)
    return Contact.objects.create(**fields)


def make_knowledge_base(tenant, **overrides):
    from core.models.knowledge_base import KnowledgeBase

    fields = {"tenant": _tenant_of(tenant), "name": f"kb-{_suffix()}"}
    fields.update(overrides)
    return KnowledgeBase.objects.create(**fields)


def make_kb_website(tenant, url=None, **overrides):
    from core.models.knowledge_base import KBWebsite

    fields = {
        "tenant": _tenant_of(tenant),
        "url": url or f"https://example.com/{_suffix()}",
    }
    fields.update(overrides)
    return KBWebsite.objects.create(**fields)


def make_kb_file(tenant, size_bytes, **overrides):
    from core.models.knowledge_base import KBFiles

    fields = {
        "tenant": _tenant_of(tenant),
        "original_name": f"file-{_suffix()}.pdf",
        "file_size_in_bytes": size_bytes,
        "extension": "pdf",
    }
    fields.update(overrides)
    return KBFiles.objects.create(**fields)


def make_campaign(tenant, agent=None, **overrides):
    from core.models.campaign import Campaign

    tenant = _tenant_of(tenant)
    fields = {
        "tenant": tenant,
        "name": f"campaign {_suffix()}",
        "agent": agent or make_agent(tenant),
    }
    fields.update(overrides)
    return Campaign.objects.create(**fields)


# --------------------------------------------------------------------------- #
# Payments
# --------------------------------------------------------------------------- #

def make_transaction(tenant=None, plan=None, **overrides):
    """A paid order. The order id is what every credit grant is keyed on."""
    from onboard.models import Transaction

    fields = {
        "razorpay_order_id": f"order_{_suffix()}{_suffix()[:4]}",
        "amount": Decimal("1180.00"),
        "status": Transaction.TransactionStatusChoices.SUCCESSFUL,
        "transaction_type": Transaction.TransactionTypeChoices.ONBOARDING,
        "tenant": _tenant_of(tenant) if tenant is not None else None,
        "plan": plan,
    }
    fields.update(overrides)
    return Transaction.objects.create(**fields)


def pay(order_id, *, event_id=None, payment_id=None, client=None):
    """Deliver a ``payment.captured`` webhook for an order.

    Unauthenticated on purpose — that is how Razorpay calls it, and a test that
    authenticates this request is not testing the webhook.
    """
    return (client or APIClient()).post(
        WEBHOOK_URL,
        {
            "id": event_id or f"evt_{_suffix()}{_suffix()[:4]}",
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": payment_id or f"pay_{_suffix()}",
                        "order_id": order_id,
                    }
                }
            },
        },
        format="json",
    )


# --------------------------------------------------------------------------- #
# Invariants
# --------------------------------------------------------------------------- #

def ledger_sums(tenant):
    """``(total, allocated, purchased)`` summed straight from the ledger."""
    rows = CreditLedger.all_objects.filter(tenant_id=_tenant_id(tenant))

    def total(**filters):
        return rows.filter(**filters).aggregate(n=Sum("amount"))["n"] or 0

    return (
        total(),
        total(bucket=CreditLedger.Bucket.ALLOCATED),
        total(bucket=CreditLedger.Bucket.PURCHASED),
    )


def assert_invariant(test_case, tenant, *, msg=""):
    """``SUM(ledger) == Wallet.balance``, per bucket.

    Run this at the end of every test that moves credits. It is the assertion most
    likely to catch a regression nobody predicted: a service that updates a balance
    without writing a row, or writes a row against the wrong bucket, satisfies every
    targeted assertion in its own test and fails here.
    """
    tenant_id = _tenant_id(tenant)
    wallet = Wallet.objects.get(tenant_id=tenant_id)
    total, allocated, purchased = ledger_sums(tenant_id)
    context = f" ({msg})" if msg else ""

    test_case.assertEqual(
        wallet.allocated_balance, allocated,
        f"allocated bucket drifted from its ledger rows{context}",
    )
    test_case.assertEqual(
        wallet.purchased_balance, purchased,
        f"purchased bucket drifted from its ledger rows{context}",
    )
    test_case.assertEqual(
        wallet.balance, total,
        f"balance drifted from the ledger{context}",
    )
    # The database enforces this too, but asserting it here names the failure.
    test_case.assertEqual(
        wallet.balance, wallet.allocated_balance + wallet.purchased_balance,
        f"balance is not the sum of its buckets{context}",
    )
    return wallet


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #

def _tenant_of(value):
    return value.tenant if isinstance(value, TenantContext) else value


def _tenant_id(value):
    value = _tenant_of(value)
    return getattr(value, "id", value)
