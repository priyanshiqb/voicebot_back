"""Duplicating a plan — all three models, as a new draft.

**Why this is not a convenience.** A yearly plan is a *separate plan* carrying 31
mostly-identical fields. Retyping them is exactly how a yearly plan ends up with a
monthly plan's add-on prices — and block prices are charged **per cycle**, so ₹199 per
10 GB buys a month on one plan and a whole year on the other. The same storage at a
twelfth of the price, from a copy-paste nobody reviewed.

The readiness service warns about that afterwards (``YEARLY_ADDONS_LOOK_MONTHLY``).
This is what stops it happening.
"""

from django.db import transaction
from django.utils import timezone

from onboard.models import Plan, PlanEntitlement
from ops.services.plan import config
from wallet.models import CreditRate

#: Never copied. `code` and `name` are unique and must be decided; `status` starts at
#: draft whatever the source was; the audit columns belong to the new row.
NOT_COPIED = {
    "id", "code", "name", "status", "is_active", "is_deleted", "deleted_at",
    "created_at", "updated_at", "created_by", "updated_by", "deleted_by",
}


def _copyable_fields(model, extra_skip=()):
    skip = NOT_COPIED | set(extra_skip)
    return [
        f.attname
        for f in model._meta.concrete_fields
        if f.attname not in skip and f.name not in skip
    ]


@transaction.atomic
def clone(source, *, code, name, billing_cycle=None, sort_order=None):
    """Copy ``source`` into a new draft, optionally switching billing cycle.

    Returns ``(plan, review)`` where ``review`` names what a human still has to look
    at. **Prices are copied verbatim, never converted.** Turning ₹199/month into
    ₹2,388/year is a commercial decision — twelve times is arithmetic, but whether the
    annual price should carry the same discount as the plan is not. Guessing would be
    worse than the retyping this exists to prevent, because a wrong number that
    somebody believes was calculated is harder to spot than a blank one.
    """
    fields = {f: getattr(source, f) for f in _copyable_fields(Plan)}
    fields.update(code=code, name=name, status=Plan.Status.DRAFT)
    if billing_cycle is not None:
        fields["billing_cycle"] = billing_cycle
    if sort_order is not None:
        fields["sort_order"] = sort_order

    plan = Plan.objects.create(**fields)

    entitlement_fields = _copyable_fields(PlanEntitlement, extra_skip={"plan_id"})
    PlanEntitlement.objects.bulk_create([
        PlanEntitlement(
            plan=plan, **{f: getattr(row, f) for f in entitlement_fields}
        )
        for row in config.live_entitlements(source)
    ])

    started = timezone.now()
    rate_fields = _copyable_fields(
        CreditRate, extra_skip={"plan_id", "effective_from", "effective_to"}
    )
    CreditRate.objects.bulk_create([
        CreditRate(
            plan=plan,
            effective_from=started,
            **{f: getattr(row, f) for f in rate_fields},
        )
        for row in config.live_rates(source)
    ])

    return plan, _review_notes(source, plan)


def _band_label(plan):
    if plan.is_fixed_quantity:
        return f"exactly {plan.min_minutes:,} minutes"
    low = f"{plan.min_minutes:,}" if plan.min_minutes is not None else "any"
    high = f"{plan.max_minutes:,}" if plan.max_minutes is not None else "any"
    return f"{low}-{high} minutes"


def _review_notes(source, plan):
    """What a human has to decide before this copy is worth activating.

    Not warnings — the readiness service covers those. These are the things a *copy*
    specifically leaves wrong, and they are worth saying at the moment of cloning
    rather than at the moment of activating, when the source is no longer in mind.
    """
    notes = []

    if plan.billing_cycle != source.billing_cycle:
        notes.append({
            "code": "CYCLE_CHANGED",
            "message": (
                f"Billing cycle changed from {source.get_billing_cycle_display()} to "
                f"{plan.get_billing_cycle_display()}. **Every price was copied as-is, "
                "not converted.** The plan price, the top-up price and every add-on "
                "block price are all charged per cycle, so each one now buys a "
                "different amount of time than it did on the original."
            ),
        })

        priced = [
            row.resource
            for row in config.live_entitlements(plan)
            if row.is_addon_available and row.price_per_additional
        ]
        if priced:
            notes.append({
                "code": "ADDON_PRICES_NEED_REVIEW",
                "resources": sorted(priced),
                "message": (
                    "These add-ons carry the original plan's block prices. On a "
                    "different cycle that is the same capacity for a different amount "
                    "of money — usually by a factor of twelve."
                ),
            })

    if plan.credit_basis and plan.min_minutes == source.min_minutes             and plan.max_minutes == source.max_minutes:
        if plan.billing_cycle == source.billing_cycle:
            notes.append({
                "code": "BAND_COLLIDES",
                "message": (
                    f"Both plans sell the same minutes "
                    f"({_band_label(plan)}). Harmless while this is a draft, and "
                    f"refused at activation — two plans claiming one quantity give "
                    f"the pricing page a coin-flip between two prices."
                ),
            })
        else:
            notes.append({
                "code": "BAND_NEEDS_CONVERSION",
                "message": (
                    f"The band ({_band_label(plan)}) was copied unchanged onto a "
                    f"different billing cycle. {plan.get_billing_cycle_display()} "
                    f"minutes are not {source.get_billing_cycle_display()} minutes, "
                    f"and the rupees-per-minute prices came across verbatim too — so "
                    f"the same figure now buys a very different amount of time."
                ),
            })

    if plan.sort_order == source.sort_order:
        notes.append({
            "code": "SORT_ORDER_COLLIDES",
            "message": (
                f"Both plans sit at sort order {plan.sort_order}, so the pricing page "
                "will order them arbitrarily against each other."
            ),
        })

    return notes
