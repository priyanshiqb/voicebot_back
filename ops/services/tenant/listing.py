"""The tenant list, in two queries at any page size.

**Every per-tenant scalar is a correlated ``Subquery``.** The obvious annotation is
wrong here: ``ops.services.plan.config.annotated_queryset`` gets away with
``Count(..., distinct=True)`` because it only ever counts, while this list needs
values from a *specific* related row — the balance from the wallet, the plan name
from the active subscription. Joining four one-to-many relations in one ``SELECT``
multiplies rows before any aggregate sees them, and ``DISTINCT`` cannot put a
multiplied scalar back together.

Subqueries cost no extra round trips: they are part of the one statement, and none of
them can duplicate a row.
"""

from django.db.models import Exists, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce

from core.models.agent_call_log import AgentCallLog
from onboard.models import Subscription
from tenant.models import Tenant, TenantSettings
from wallet.models import Wallet

Status = Subscription.SubscriptionStatusChoices

#: Sort keys the console may ask for, mapped to what they order by. A whitelist
#: rather than passing `ordering` through: an unbounded `order_by` on a
#: user-supplied string is a way to sort by a column that is not indexed, on a table
#: that will not stay small.
ORDERING = {
    "created_at": "created_at",
    "-created_at": "-created_at",
    "name": "name",
    "-name": "-name",
    "balance": "balance",
    "-balance": "-balance",
    "last_call_at": "last_call_at",
    "-last_call_at": "-last_call_at",
    "cycle_ends": "cycle_ends",
    "-cycle_ends": "-cycle_ends",
}

DEFAULT_ORDERING = "-created_at"


def annotated_queryset(*, include_deleted=False):
    """Tenants with every list column attached, and no per-row query.

    ``plan_snapshot__name``, not ``plan__name``: the snapshot is the contract. A
    tenant on a plan renamed last week is still on what they bought, and this list
    has to agree with the 360 — which reads the snapshot — or support chases a
    discrepancy that is not one.
    """
    live = Subscription.objects.filter(
        tenant=OuterRef("pk"), status=Status.ACTIVE, is_deleted=False,
    )
    # The lapsed fallback, mirroring `subscriptions.current_or_expired`: cancelled
    # rows are excluded because a plan change superseded them, so the row that
    # replaced them is the one that matters. Without this the list would file every
    # expired tenant under "no subscription", which is the opposite of what support
    # is looking for — "never signed up" and "cycle ended" are different problems.
    lapsed = (
        Subscription.objects.filter(tenant=OuterRef("pk"))
        .exclude(status=Status.CANCELLED)
        .order_by("-end_date")
    )

    def current(field):
        return Coalesce(Subquery(live.values(field)[:1]), Subquery(lapsed.values(field)[:1]))

    wallet = Wallet.objects.filter(tenant=OuterRef("pk"), is_deleted=False)
    settings_row = TenantSettings.objects.filter(tenant=OuterRef("pk"), is_deleted=False)
    last_call = AgentCallLog.objects.filter(tenant=OuterRef("pk")).order_by("-started_at")

    base = Tenant.all_objects if include_deleted else Tenant.objects

    return base.select_related("timezone", "language").annotate(
        plan_name=current("plan_snapshot__name"),
        plan_code=current("plan_snapshot__code"),
        live_plan_name=current("plan__name"),
        live_plan_code=current("plan__code"),
        subscription_status=current("status"),
        cycle_ends=current("end_date"),
        has_scheduled_change=Exists(live.filter(scheduled_plan__isnull=False)),
        balance=Subquery(wallet.values("balance")[:1]),
        wallet_frozen=Subquery(wallet.values("is_frozen")[:1]),
        credits_enforced=Subquery(settings_row.values("credits_enforced")[:1]),
        entitlements_enforced=Subquery(settings_row.values("entitlements_enforced")[:1]),
        last_call_at=Subquery(last_call.values("started_at")[:1]),
    )


def _bool(value):
    """A query-string boolean, or ``None`` when the param was not supplied."""
    if value is None or value == "":
        return None
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _not_true(field):
    """"False, or no row at all" — as a ``Q``, because ``exclude`` gets this wrong.

    These annotations are subqueries over a *related* row, so a tenant with no wallet
    and no settings row annotates as ``NULL``, not ``False``. ``.exclude(x=True)``
    compiles to ``NOT (x = true)``, which is ``NULL`` for a ``NULL`` — never ``TRUE``
    — so those tenants are silently dropped from the result.

    That is the wrong direction to fail in: a tenant with no wallet is exactly the
    half-onboarded account somebody filtering for "not frozen" is looking for.
    """
    return Q(**{field: False}) | Q(**{f"{field}__isnull": True})


def apply_filters(queryset, params):
    """Narrow the list from query parameters. Unknown params are ignored."""
    q = (params.get("q") or "").strip()
    if q:
        # `.distinct()` on this branch only. A tenant with three users matching the
        # term joins to three rows — but forcing DISTINCT over the whole annotated
        # select on every request that does *not* search would be paying for it
        # everywhere to fix it here.
        queryset = queryset.filter(
            Q(name__icontains=q)
            | Q(subdomain__icontains=q)
            | Q(user_mappings__user__email__icontains=q)
        ).distinct()

    plan = (params.get("plan") or "").strip()
    if plan:
        # Matched on the live subscription's snapshot first, falling back to the FK
        # for rows written before snapshots existed.
        queryset = queryset.filter(
            Q(plan_code=plan) | Q(plan_code__isnull=True, live_plan_code=plan)
        )

    status_param = (params.get("status") or "").strip().lower()
    if status_param == "none":
        # The filter that finds the interesting rows: no subscription at all means
        # mid-onboarding or a payment that never completed, and neither shows up
        # under any other value.
        queryset = queryset.filter(subscription_status__isnull=True)
    elif status_param in set(Status.values):
        queryset = queryset.filter(subscription_status=status_param)

    enforcement = (params.get("enforcement") or "").strip().lower()
    if enforcement == "credits":
        queryset = queryset.filter(credits_enforced=True)
    elif enforcement == "entitlements":
        queryset = queryset.filter(entitlements_enforced=True)
    elif enforcement == "both":
        queryset = queryset.filter(credits_enforced=True, entitlements_enforced=True)
    elif enforcement == "neither":
        queryset = queryset.filter(_not_true("credits_enforced")).filter(
            _not_true("entitlements_enforced")
        )

    frozen = _bool(params.get("frozen"))
    if frozen is True:
        queryset = queryset.filter(wallet_frozen=True)
    elif frozen is False:
        queryset = queryset.filter(_not_true("wallet_frozen"))

    is_active = _bool(params.get("is_active"))
    if is_active is not None:
        queryset = queryset.filter(is_active=is_active)

    return queryset


def apply_ordering(queryset, params):
    """Order by a whitelisted key, newest first by default."""
    requested = (params.get("ordering") or "").strip()
    return queryset.order_by(ORDERING.get(requested, DEFAULT_ORDERING))


def build(params):
    """The whole list query for one request."""
    queryset = annotated_queryset(
        include_deleted=bool(_bool(params.get("include_deleted")))
    )
    return apply_ordering(apply_filters(queryset, params), params)
