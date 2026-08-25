"""What a tenant may hold, and what they have bought the right to hold.

Entitlements are **gauges** — held, then released — so nothing here ever touches the
credit wallet. Buying an agent seat changes what the tenant may hold and what they pay
each cycle; it produces no ledger entry, and deleting an agent frees the allowance
without a refund question. That is the whole reason they are not credits: a credit
spent is gone, whereas a seat given back is available again.

Two halves:

* recording purchases (:func:`apply_addon`, :func:`recurring_total`)
* enforcing limits (:func:`check`, :func:`usage`)

The limit for a resource is ``included + purchased``, where *included* comes from the
subscription's frozen snapshot — so raising a plan's caps does not retroactively
change what an existing subscriber agreed to.
"""

import logging

from django.db import transaction as db_transaction
from django.db.models import Sum
from django.utils import timezone

logger = logging.getLogger(__name__)


class QuotaExceeded(Exception):
    """The tenant is at their limit for a resource.

    Carries the numbers so a view can render something actionable rather than a bare
    refusal — "3 of 3 agents used" tells someone what to do next; "forbidden" does not.
    """

    def __init__(self, resource, *, used, limit, requested=1):
        self.resource = resource
        self.used = used
        self.limit = limit
        self.requested = requested
        super().__init__(
            f"{resource}: {used} of {limit} in use; cannot add {requested} more."
        )


@db_transaction.atomic
def apply_addon(*, tenant_id, subscription, resource, additional_units, notes=""):
    """Add purchased capacity, freezing the price it recurs at.

    Buying more re-prices the whole block to the current catalogue price, which is how
    seat billing works everywhere: "you have 5 extra agents at Rs 299 each", not five
    rows at four historical prices. The tenant is quoted that way and invoiced that
    way.
    """
    from onboard.models import PlanEntitlement, TenantEntitlement

    catalogue = PlanEntitlement.objects.filter(
        plan=subscription.plan, resource=resource, is_deleted=False
    ).first()
    if catalogue is None:
        logger.error(
            "cannot apply %s add-on for tenant %s: plan %s has no such allowance",
            resource, tenant_id, subscription.plan.code,
        )
        return None

    row, _ = TenantEntitlement.objects.select_for_update().get_or_create(
        tenant_id=tenant_id,
        resource=resource,
        is_deleted=False,
        defaults={"additional_quantity": 0},
    )
    row.additional_quantity += int(additional_units)
    row.price_per_unit = catalogue.price_per_additional
    row.addon_step = catalogue.addon_step
    row.subscription = subscription
    row.purchased_at = timezone.now()
    if notes:
        row.notes = notes
    row.save()

    logger.info(
        "tenant %s now holds %s extra %s at %s per block",
        tenant_id, row.additional_quantity, resource, row.price_per_unit,
    )
    return row


def recurring_total(tenant_id):
    """Pre-tax rupees the tenant's add-ons add to every cycle.

    Renewal and proration both need this: a renewal order is the plan's price plus
    whatever capacity the tenant is still renting.
    """
    from decimal import Decimal

    from onboard.models import TenantEntitlement

    total = Decimal("0.00")
    for row in TenantEntitlement.objects.filter(tenant_id=tenant_id, is_deleted=False):
        total += row.recurring_charge
    return total


def summary(tenant_id, subscription, *, with_usage=False):
    """Included, purchased and effective limits for every resource.

    Effective limit is ``included + purchased``, with ``None`` meaning unlimited.
    ``with_usage`` adds the live count, which costs one query per resource — fine for
    a settings page, not for a hot path.
    """
    from onboard.constants.entitlement_resource import EntitlementResource
    from onboard.models import TenantEntitlement

    extras = {
        row.resource: row
        for row in TenantEntitlement.objects.filter(
            tenant_id=tenant_id, is_deleted=False
        )
    }

    out = []
    for resource in EntitlementResource.values:
        included = subscription.included_entitlement(resource)
        row = extras.get(resource)
        extra = row.additional_quantity if row else 0
        entry = {
            "resource": resource,
            "included": included,
            "additional": extra,
            "limit": None if included is None else included + extra,
            "recurring_charge": str(row.recurring_charge) if row else "0.00",
        }
        if with_usage:
            used = current_usage(tenant_id, resource, subscription=subscription)
            entry["used"] = used
            entry["remaining"] = (
                None if entry["limit"] is None else max(entry["limit"] - used, 0)
            )
        out.append(entry)
    return out


# --------------------------------------------------------------------------- #
# Enforcement
# --------------------------------------------------------------------------- #

def current_usage(tenant_id, resource, subscription=None):
    """How much of a resource the tenant is using.

    Two different questions, depending on how the plan configures the allowance.

    **A standing total** (``reset_period = none``) is counted live from the rows that
    *are* the resource. No counter to drift, no reset job to forget — deleting an
    agent frees the allowance the moment it happens, because there was never a
    separate number recording that it was taken.

    **A windowed allowance** counts *acts performed* since the window opened, and
    deleting the row afterwards does not give the allowance back. ``subscription`` is
    what supplies the window; without it a windowed allowance falls back to counting
    like a standing total, which is the lenient direction to fail in.
    """
    from onboard.constants.entitlement_resource import EntitlementResource as R
    from onboard.constants.reset_period import is_windowed, window_start

    since = None
    if subscription is not None:
        period = subscription.entitlement_reset_period(resource)
        if is_windowed(period):
            # Anchored to when the subscription began, not to when the current cycle
            # began: renewal pushes start_date forward, which would reset a quarterly
            # window every month on a monthly plan.
            anchor = subscription.created_at or subscription.start_date
            since = window_start(period, anchor, timezone.now())

    if since is not None:
        return _windowed_usage(tenant_id, resource, since)

    if resource == R.AGENT:
        from core.models.agent import Agent
        return Agent.objects.filter(tenant_id=tenant_id).count()

    if resource == R.USER:
        from tenant.models import TenantUserMapping
        return TenantUserMapping.objects.filter(tenant_id=tenant_id, is_deleted=False).count()

    if resource == R.CONTACT:
        from core.models.contact import Contact
        return Contact.objects.filter(tenant_id=tenant_id).count()

    if resource == R.KNOWLEDGE_BASE:
        from core.models.knowledge_base import KnowledgeBase
        return KnowledgeBase.objects.filter(tenant_id=tenant_id).count()

    if resource == R.CRAWL_PAGE:
        from core.models.knowledge_base import KBWebsite
        return KBWebsite.objects.filter(tenant_id=tenant_id).count()

    if resource == R.CAMPAIGN_MONTHLY:
        # Reached only when the plan leaves this allowance as a standing total, which
        # is an odd way to configure it — a campaign is an act, not a holding. Same
        # rule as the windowed counter, without the window.
        from core.models.campaign import Campaign
        return (
            Campaign.all_objects.filter(tenant_id=tenant_id)
            .filter(_campaign_consumes_allowance())
            .count()
        )

    if resource == R.STORAGE_GB:
        from core.models.knowledge_base import KBFiles
        total_bytes = KBFiles.objects.filter(tenant_id=tenant_id).aggregate(
            size=Sum("file_size_in_bytes")
        )["size"] or 0
        # Rounded up: a tenant holding 10.2 GB on a 10 GB plan is over, and reporting
        # 10 would make the limit look satisfied while the disk says otherwise.
        return -(-total_bytes // (1024 ** 3))

    logger.warning("no usage counter for resource %s", resource)
    return 0


def _campaign_consumes_allowance():
    """Q for a campaign that is holding an allowance slot.

    A campaign counts if it **ever ran** — ``started_at`` is stamped once, in the
    same save that sets RUNNING, and nothing can take that back. Deleting a
    campaign that dialled somebody must not reclaim the slot; the calls happened.

    It also counts while it is still *pending* — a draft or a scheduled campaign
    is a claim on the allowance, so the limit is felt when a tenant sets one up
    rather than after they have built an audience they cannot call.

    It stops counting when it never ran **and** is no longer going to. There are
    three routes to that and they are the same fact:

    * deleted before it started — a mistyped draft thrown away
    * cancelled before it started — the tenant changed their mind
    * failed to materialise — the bot was gone, or the audience resolved to nobody

    In none of those did a call go out, so in none of them has the tenant had
    anything. Keying the exemption on ``started_at`` rather than on how the
    campaign ended is what keeps that honest: a tenant cannot cancel their way to
    a free allowance, because reclaiming the slot means passing this check again
    on relaunch.
    """
    from django.db.models import Q

    from core.models.campaign import Campaign

    return Q(started_at__isnull=False) | (
        Q(is_deleted=False) & ~Q(status__in=Campaign.TERMINAL_STATUSES)
    )


def _windowed_usage(tenant_id, resource, since):
    """Acts performed since the window opened.

    **Counted from ``all_objects``, including soft-deleted rows.** That is the whole
    point: the act happened, and removing the record of it must not hand the
    allowance back. Counting through the default manager is exactly the hole this
    replaced — a tenant could run their monthly campaigns, delete them, and run the
    same number again.
    """
    from onboard.constants.entitlement_resource import EntitlementResource as R

    if resource == R.CAMPAIGN_MONTHLY:
        from core.models.campaign import Campaign

        # Counted from `created_at`, so a tenant cannot stockpile drafts in one
        # window and fire them all the moment the next one opens.
        return (
            Campaign.all_objects.filter(tenant_id=tenant_id, created_at__gte=since)
            .filter(_campaign_consumes_allowance())
            .count()
        )

    model = _model_for(resource)
    if model is None:
        logger.warning("no windowed counter for resource %s", resource)
        return 0

    # Every other windowed resource counts creations outright, including deleted
    # rows. Only a campaign can tell a discarded draft from a used allowance, because
    # only a campaign records whether it ran; elsewhere there is no way to separate a
    # mistake from a create-and-delete loop, so the act counts.
    manager = getattr(model, "all_objects", model.objects)
    return manager.filter(tenant_id=tenant_id, created_at__gte=since).count()


def _model_for(resource):
    """The model whose rows *are* a resource, for the generic windowed counter."""
    from onboard.constants.entitlement_resource import EntitlementResource as R

    if resource == R.AGENT:
        from core.models.agent import Agent
        return Agent
    if resource == R.USER:
        from tenant.models import TenantUserMapping
        return TenantUserMapping
    if resource == R.CONTACT:
        from core.models.contact import Contact
        return Contact
    if resource == R.KNOWLEDGE_BASE:
        from core.models.knowledge_base import KnowledgeBase
        return KnowledgeBase
    if resource == R.CRAWL_PAGE:
        from core.models.knowledge_base import KBWebsite
        return KBWebsite
    if resource == R.STORAGE_GB:
        # Bytes held, not files created — a windowed storage allowance would be
        # measuring the wrong thing entirely, so it is refused rather than guessed.
        return None
    return None


def limit_for(tenant_id, subscription, resource):
    """Effective limit, or ``None`` for unlimited."""
    from onboard.models import TenantEntitlement

    included = subscription.included_entitlement(resource)
    if included is None:
        return None

    extra = (
        TenantEntitlement.objects.filter(
            tenant_id=tenant_id, resource=resource, is_deleted=False
        )
        .values_list("additional_quantity", flat=True)
        .first()
        or 0
    )
    return included + extra


def is_enforced(tenant_id):
    """Whether exceeding a limit actually blocks the tenant.

    Off by default, and for the same reason credits meter before they block: switching
    caps on for tenants who are *already* over them would break working accounts with
    no warning. Turn it on per tenant once their usage has been checked.
    """
    from tenant.models import TenantSettings

    return bool(
        TenantSettings.objects.filter(tenant_id=tenant_id)
        .values_list("entitlements_enforced", flat=True)
        .first()
    )


def check(tenant, resource, *, requested=1, subscription=None):
    """Refuse to exceed a limit — but only where enforcement is switched on.

    Raises :class:`QuotaExceeded`. While enforcement is off it logs what *would* have
    been refused and returns, so the first thing anyone learns about a tenant living
    beyond their plan is a log line rather than a broken workflow.

    Never raises anything else: a quota lookup must not be why creating an agent
    fails.
    """
    from onboard.constants.entitlement_resource import EntitlementResource
    from onboard.services import subscriptions as subscription_service

    tenant_id = getattr(tenant, "id", tenant)

    try:
        if resource not in EntitlementResource.values:
            # Allowed, but never silently: a guard configured with a misspelled
            # resource would otherwise read as "unlimited" and disable that cap
            # permanently, with nothing anywhere to say so.
            logger.warning(
                "entitlement check for unknown resource %r (tenant %s); allowing",
                resource, tenant_id,
            )
            return

        subscription = subscription or subscription_service.current_for(tenant_id)
        if subscription is None:
            # No live plan means no agreed limits. Whatever gates access in that state
            # is the subscription's business, not the quota checker's.
            return

        limit = limit_for(tenant_id, subscription, resource)
        if limit is None:
            return

        used = current_usage(tenant_id, resource, subscription=subscription)
        if used + requested <= limit:
            return

        if not is_enforced(tenant_id):
            logger.warning(
                "would refuse %s for tenant %s: %s of %s in use",
                resource, tenant_id, used, limit,
            )
            return

        raise QuotaExceeded(resource, used=used, limit=limit, requested=requested)

    except QuotaExceeded:
        raise
    except Exception:
        logger.exception(
            "entitlement check failed for tenant %s / %s; allowing", tenant_id, resource
        )
