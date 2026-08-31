"""The 360 — everything about one tenant, in one payload.

Three requests to render one screen is also three chances to render it half-loaded,
which is the same reason Module 1's plan editor loads a whole plan at once.

**Every block degrades on its own.** A tenant with no subscription, no wallet and no
settings row is not an edge case here — it is a signup whose payment never cleared,
which is exactly the tenant support is looking at. A 360 that raises on that case
fails at the moment it is needed most, so each block returns its "nothing configured"
shape and says so rather than letting the request 500.

**Nothing in this module calls a ``for_tenant()`` helper.** Both
``TenantSettings.for_tenant`` and ``Wallet.for_tenant`` are ``get_or_create`` — they
exist so the *call path* cannot fail on a missing row, and using them here would mean
a ``GET`` writes rows. Reads use an explicit filter and fall back to the model's own
defaults.
"""

from onboard.models import TenantEntitlement, Transaction
from onboard.services import entitlements as entitlement_service
from onboard.services import subscriptions as subscription_service
from ops.services.tenant import health as health_service
from ops.services.tenant import usage as usage_service
from tenant.models import TenantSettings, TenantUserMapping
from wallet.models import CreditLedger, Wallet

#: How many rows of each history the screen carries. Enough to see a pattern, few
#: enough that the payload stays one screenful — the full history is what a dedicated
#: payments or ledger screen is for.
PAYMENT_ROWS = 10
LEDGER_ROWS = 20


def owner_of(tenant_id):
    """The tenant's owner, and how many users they have.

    **There is no owner field.** ``TenantUserMapping`` is a bare 1:1 with no role and
    no flag, and ``create_tenant_and_mapping`` maps the signup user exactly like every
    user added afterwards. The earliest mapping is therefore the account holder for
    every tenant created through the signup flow, which is all of them.

    Reported as derived rather than as fact, so nobody builds an ownership transfer on
    an inference. When something needs to *depend* on the answer, that is the moment
    to add the field.
    """
    mapping = (
        TenantUserMapping.objects.filter(tenant_id=tenant_id)
        .select_related("user")
        .order_by("created_at")
        .first()
    )
    count = TenantUserMapping.objects.filter(tenant_id=tenant_id).count()
    return mapping.user if mapping else None, count


def settings_of(tenant_id):
    """The tenant's settings row, or ``None``. Never creates one."""
    return TenantSettings.objects.filter(tenant_id=tenant_id, is_deleted=False).first()


def wallet_of(tenant_id):
    """The tenant's wallet, or ``None``. Never creates one."""
    return (
        Wallet.objects.filter(tenant_id=tenant_id, is_deleted=False)
        .select_related("current_plan")
        .first()
    )


def subscription_of(tenant):
    """The live subscription, or the most recent lapsed one.

    ``current_or_expired``, not ``current_for``: a tenant whose cycle ended still has
    a relationship with us, and the lapsed row is precisely what support needs when
    the ticket is "why can't I make calls?".
    """
    tenant_id = getattr(tenant, "id", tenant)
    subscription = subscription_service.current_or_expired(tenant_id)
    if subscription is None:
        return None
    # Loaded here rather than in the serializer so the block costs one query whether
    # or not a scheduled change exists.
    return (
        type(subscription).objects
        .select_related("plan", "scheduled_plan", "superseded_by")
        .get(pk=subscription.pk)
    )


def entitlements_of(tenant_id, subscription):
    """Included, purchased, effective limit and live usage for all 7 resources.

    ``with_usage=True`` costs one query per resource. Its own docstring calls that
    "fine for a settings page, not for a hot path" — a 360 opened by a human for one
    tenant is a settings page.

    Without a subscription there is no included allowance to report: ``summary``
    dereferences ``subscription.included_entitlement`` and would raise. The block
    reports why instead, because a tenant stuck mid-onboarding is one of the cases
    this screen exists for.
    """
    if subscription is None:
        extras = list(
            TenantEntitlement.objects.filter(tenant_id=tenant_id, is_deleted=False)
            .values("resource", "additional_quantity")
        )
        return {
            "rows": [],
            "reason": "no_subscription",
            "purchased_extras": extras,
        }
    return {
        "rows": entitlement_service.summary(tenant_id, subscription, with_usage=True),
        "reason": None,
        "purchased_extras": None,
    }


def payments_of(tenant_id):
    """The last few orders, newest first.

    **The tenant's first payment is not here, and cannot be.** An onboarding
    transaction has ``tenant = NULL`` by check constraint — the tenant does not exist
    until that payment clears — so it is reachable through ``temp_user`` only. The
    serializer says so on screen rather than letting a customer count one more
    payment than the console shows.
    """
    return list(
        Transaction.objects.filter(tenant_id=tenant_id)
        .select_related("plan", "target_plan")
        .order_by("-created_at")[:PAYMENT_ROWS]
    )


def ledger_of(tenant_id):
    """The last few credit movements, with the rating that produced each.

    Read from ``all_objects``. The ledger is append-only and its manager filters
    soft-deleted rows — a row that has somehow been deleted is the single most
    important row on this screen.
    """
    return list(
        CreditLedger.all_objects.filter(tenant_id=tenant_id)
        .select_related("subscription")
        .order_by("-created_at")[:LEDGER_ROWS]
    )


def assemble(tenant):
    """Every block, for one tenant. Roughly 23 queries.

    That is the honest cost of eight sources assembled without lying about any of
    them, and it is fine: one screen, one tenant, opened by a human. What is not fine
    is any of it running per row in the list — see ``ops.services.tenant.listing``.
    """
    tenant_id = tenant.id

    owner, user_count = owner_of(tenant_id)
    settings_row = settings_of(tenant_id)
    subscription = subscription_of(tenant_id)
    wallet = wallet_of(tenant_id)

    return {
        "tenant": tenant,
        "owner": owner,
        "user_count": user_count,
        "settings": settings_row,
        "subscription": subscription,
        "wallet": wallet,
        "entitlements": entitlements_of(tenant_id, subscription),
        "payments": payments_of(tenant_id),
        "ledger": ledger_of(tenant_id),
        "usage": usage_service.report(tenant_id),
        "health": health_service.report(
            tenant_id, subscription=subscription, wallet=wallet
        ),
    }
