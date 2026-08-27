"""Creating and reading a whole plan — three models, one operation.

Everything here is atomic. A plan is only meaningful as a complete set, so a create
that wrote the plan row and then failed on the fourth entitlement would leave behind
precisely the half-configured thing this module exists to prevent.
"""

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from onboard.models import Plan, PlanEntitlement, Subscription
from wallet.models import CreditRate

#: Subscriptions that mean "somebody is on this plan right now". Cancelled and expired
#: rows are history — they say what a plan *was* used for, not who would be affected
#: by editing it.
#:
#: `TRIALING` counts. A tenant on a trial is on the plan, sees its limits and will be
#: billed by its terms — leaving them out would under-report the impact of an edit to
#: precisely the tenants most likely to be evaluating it.
LIVE_SUBSCRIPTION_STATUSES = [
    Subscription.SubscriptionStatusChoices.ACTIVE,
    Subscription.SubscriptionStatusChoices.TRIALING,
]


def annotated_queryset():
    """Plans with the counts the list screen needs, without a query per row.

    ``distinct=True`` on each: joining entitlements and rates in one query multiplies
    the rows, and counting the product rather than the set would report 21 allowances
    on a correctly configured plan.
    """
    return (
        Plan.objects.filter(is_deleted=False)
        .annotate(
            entitlements_configured=Count(
                "entitlements",
                filter=Q(entitlements__is_deleted=False),
                distinct=True,
            ),
            rates_configured=Count(
                "credit_rates",
                filter=Q(
                    credit_rates__is_deleted=False,
                    credit_rates__effective_to__isnull=True,
                ),
                distinct=True,
            ),
            subscriber_count=Count(
                "subscriptions",
                filter=Q(
                    subscriptions__status__in=LIVE_SUBSCRIPTION_STATUSES,
                    subscriptions__is_deleted=False,
                ),
                distinct=True,
            ),
        )
        .order_by("sort_order", "-created_at")
    )


def subscriber_count(plan):
    """How many tenants are on this plan right now."""
    return Subscription.objects.filter(
        plan=plan,
        status__in=LIVE_SUBSCRIPTION_STATUSES,
        is_deleted=False,
    ).count()


def live_entitlements(plan):
    return list(plan.entitlements.filter(is_deleted=False).order_by("resource"))


def live_rates(plan):
    """The open row per operation — the card that actually prices calls.

    A closed row is history and must not appear as configuration; it is what a call
    last month was priced at, not what the next one will be.
    """
    return list(
        plan.credit_rates.filter(
            is_deleted=False, effective_to__isnull=True
        ).order_by("operation")
    )


@transaction.atomic
def create(*, plan_fields, entitlements, rates):
    """Create a plan and its 11 supporting rows, as a draft.

    Draft regardless of what was sent — ``status`` is read-only on the serializer, so
    there is no way to ask for anything else. Complete is not the same as correct, and
    somebody still has to look at it before it reaches a customer.
    """
    plan = Plan.objects.create(**plan_fields)

    PlanEntitlement.objects.bulk_create([
        PlanEntitlement(plan=plan, **row) for row in entitlements
    ])

    started = timezone.now()
    CreditRate.objects.bulk_create([
        CreditRate(plan=plan, effective_from=started, **row) for row in rates
    ])

    return plan


@transaction.atomic
def replace_entitlements(plan, rows):
    """Upsert the whole capacity grid.

    Updates in place rather than delete-and-recreate. ``PlanEntitlement`` is not
    versioned and nothing points at a row by id, but recreating would churn primary
    keys that the audit log and any future reference would name — and
    ``uniq_entitlement_per_plan_resource`` would refuse the insert before the delete
    committed anyway.
    """
    existing = {row.resource: row for row in plan.entitlements.filter(is_deleted=False)}

    for row in rows:
        resource = row["resource"]
        current = existing.get(resource)
        if current is None:
            PlanEntitlement.objects.create(plan=plan, **row)
            continue
        for field, value in row.items():
            setattr(current, field, value)
        current.save()

    return live_entitlements(plan)


@transaction.atomic
def replace_rates(plan, rows, *, supersede):
    """Set the rate card.

    ``supersede=False`` edits the open rows in place. Only safe on a draft, where no
    wallet carries a stamp of them — nothing has been priced yet, so there is no
    history to preserve.

    ``supersede=True`` closes each open row and inserts a replacement, which is what
    keeps every historical charge reproducible. Existing subscribers are unaffected
    either way: their card was frozen onto their wallet when they subscribed, so a
    rate change never re-prices a call already made.
    """
    now = timezone.now()
    open_rows = {row.operation: row for row in live_rates(plan)}

    for row in rows:
        operation = row["operation"]
        current = open_rows.get(operation)

        if current is None:
            CreditRate.objects.create(plan=plan, effective_from=now, **row)
            continue

        if not supersede:
            for field, value in row.items():
                setattr(current, field, value)
            current.save()
            continue

        if not _rate_changed(current, row):
            # Superseding an unchanged rate would fill the history with rows that say
            # nothing, and cost a version boundary that means nothing.
            continue

        # Closed first: `uniq_current_rate_per_plan_operation` permits one open row
        # per (plan, operation), so the insert cannot precede the close.
        current.effective_to = now
        current.save(update_fields=["effective_to", "updated_at"])
        CreditRate.objects.create(plan=plan, effective_from=now, **row)

    return live_rates(plan)


def _rate_changed(current, row):
    """Whether ``row`` actually differs from the open rate.

    ``notes`` is a nullable text column, so a row created without one holds NULL while
    the serializer sends "". Compared naively that reads as a change, and every save
    would supersede every tier — filling the history with version boundaries that mean
    nothing and, worse, making it impossible to tell a real rate change from a save.
    """
    return any(
        _normalised(getattr(current, field)) != _normalised(value)
        for field, value in row.items()
        if field != "operation"
    )


def _normalised(value):
    """NULL and empty string are the same answer in a nullable text column."""
    return "" if value is None else value
