"""
Campaign audience resolution and materialisation.

Materialisation is the moment a campaign stops being a plan and becomes a record: the
audience is frozen into CampaignMember rows, suppression is applied and *recorded*, and
the phone number each contact will be called on is copied so a later edit cannot rewrite
history.
"""

import logging
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.models.campaign import Campaign
from core.models.campaign_member import CampaignMember, suppression_reason_for
from core.models.contact import Contact
from core.services import campaign_handoff as handoff_service
from utils.querysets import batched_queryset

logger = logging.getLogger(__name__)


def resolve_audience(campaign):
    """
    The contacts this campaign would target, as a queryset.

    Ordered by pk because every consumer walks it with `batched_queryset`, which pages
    on the primary key: over an unordered queryset that can repeat or skip rows.
    Soft-deleted contacts are excluded here rather than suppressed, since a deleted
    contact was never really part of the audience.
    """
    return (
        Contact.objects.filter(
            tenant_id=campaign.tenant_id,
            is_deleted=False,
            campaigns=campaign,
        )
        .order_by("pk")
    )


def preview_audience(tenant, contact_ids):
    """
    Resolve a prospective audience without creating anything.

    Returns matched/callable counts plus the suppression breakdown, so the wizard can
    say "500 selected, 470 will be called, 30 on do-not-call" before the user commits.
    """
    contacts = Contact.objects.filter(
        tenant=tenant, is_deleted=False, id__in=contact_ids
    )

    breakdown = {}
    callable_count = 0
    matched = 0

    for contact in batched_queryset(contacts, batch_size=1000):
        matched += 1
        reason = suppression_reason_for(contact)
        if reason is None:
            callable_count += 1
        else:
            breakdown[reason] = breakdown.get(reason, 0) + 1

    return {
        "requested": len(set(contact_ids)),
        "matched": matched,
        "callable": callable_count,
        "suppressed": matched - callable_count,
        "suppressed_by_reason": breakdown,
    }


def count_callable(campaign):
    """How many of this campaign's contacts may actually be called."""
    callable_count = 0
    for contact in batched_queryset(resolve_audience(campaign), batch_size=1000):
        if suppression_reason_for(contact) is None:
            callable_count += 1
    return callable_count


def new_handoff_id():
    """
    One idempotency key per launch, reused across every delivery attempt.

    Regenerated per launch and not per attempt: a per-attempt key would make a retry
    indistinguishable from new work, and a delivery that timed out after the runner
    received it would call the whole audience twice.
    """
    return uuid.uuid4()


@transaction.atomic
def _write_batch(campaign, contacts):
    members = []
    for contact in contacts:
        reason = suppression_reason_for(contact)
        members.append(
            CampaignMember(
                # bulk_create bypasses save() and perform_create, so tenant is set
                # explicitly — the viewset's injection is not in play inside a task.
                tenant_id=campaign.tenant_id,
                campaign=campaign,
                contact=contact,
                phone_e164=contact.phone_e164,
                status=(
                    CampaignMember.Status.SUPPRESSED
                    if reason
                    else CampaignMember.Status.PENDING
                ),
                suppression_reason=reason or "",
            )
        )
    # ignore_conflicts makes a retried task idempotent against the partial unique
    # constraint rather than raising IntegrityError on every re-run.
    CampaignMember.objects.bulk_create(members, ignore_conflicts=True)
    return len(members)


def materialize_campaign(campaign_id):
    """
    Freeze the audience into CampaignMember rows and hand the campaign to the runner.

    Batched with a savepoint per batch rather than one transaction around the whole
    audience: a single bad row must not discard a 100k-member materialisation.

    Ends in HANDING_OFF when runner notification is enabled and RUNNING when it is not;
    see the branch at the bottom.
    """
    from core.tasks import notify_runner_campaign_start as notify_task

    with transaction.atomic():
        campaign = (
            Campaign.all_objects.select_for_update()
            .filter(pk=campaign_id)
            .first()
        )
        if campaign is None:
            logger.warning("materialize_campaign: campaign %s not found", campaign_id)
            return None
        if campaign.status != Campaign.Status.MATERIALIZING:
            logger.info(
                "materialize_campaign: campaign %s is %s, not MATERIALIZING; skipping",
                campaign_id,
                campaign.status,
            )
            return campaign

    max_members = getattr(settings, "CAMPAIGN_MAX_MEMBERS", 100_000)
    batch_size = getattr(settings, "CAMPAIGN_MATERIALIZE_BATCH_SIZE", 1000)

    audience = resolve_audience(campaign)
    total = audience.count()
    if total > max_members:
        return _fail(
            campaign,
            f"This campaign targets {total} contacts, above the limit of {max_members}.",
        )

    batch = []
    try:
        # Not `.iterator()`: it opens a named Postgres cursor, and pgbouncer runs in
        # transaction pooling, so the cursor is gone by the next fetch — the whole
        # materialisation died with InvalidCursorName. Keyset paging holds no cursor.
        for contact in batched_queryset(audience, batch_size=batch_size):
            batch.append(contact)
            if len(batch) >= batch_size:
                _write_batch(campaign, batch)
                batch = []
        if batch:
            _write_batch(campaign, batch)
    except Exception as exc:  # noqa: BLE001 - recorded on the campaign, then re-raised
        logger.exception("materialize_campaign failed for %s", campaign_id)
        _fail(campaign, f"Failed to build the campaign audience: {exc}")
        raise

    campaign.recompute_counts()
    campaign.refresh_from_db()

    if campaign.pending_count == 0:
        # Reachable despite the launch-time check: a contact can opt out in the gap
        # between /launch/ and this task running.
        return _fail(
            campaign,
            "Every contact in this campaign is suppressed, so there is nobody to call.",
        )

    # `started_at` is stamped here in both branches: it records when *we* began work on
    # the campaign, and it is the moment the audience snapshot is contemporaneous with.
    campaign.started_at = timezone.now()

    if handoff_service.notify_enabled():
        # HANDING_OFF, not RUNNING. RUNNING should mean the runner has been told; with
        # one state for both there is no way to tell "notified and dialling" from
        # "notify still queued" from "notify failed and nobody is dialling".
        #
        # The notify goes out as a task rather than inline because this function has
        # just written up to 100k rows, and a network timeout must not be able to
        # discard a materialisation that otherwise succeeded.
        campaign.status = Campaign.Status.HANDING_OFF
        campaign.save(update_fields=["status", "started_at", "updated_at"])
        transaction.on_commit(
            lambda: notify_task.delay(str(campaign_id))
        )
    else:
        # Flag off: exactly the previous behaviour, so this ships dark.
        campaign.status = Campaign.Status.RUNNING
        campaign.save(update_fields=["status", "started_at", "updated_at"])

    logger.info(
        "Campaign %s materialized: %s pending, %s suppressed, status %s",
        campaign_id,
        campaign.pending_count,
        campaign.suppressed_count,
        campaign.status,
    )
    return campaign


def _fail(campaign, message):
    campaign.status = Campaign.Status.FAILED
    campaign.error_message = message
    campaign.save(update_fields=["status", "error_message", "updated_at"])
    return campaign


def _within_campaign_allowance(campaign):
    """Whether the plan still allows another campaign to be started.

    **This is the authoritative check, not the one on the create endpoint.** The
    allowance limits the act of *starting* a campaign, and this is where that act
    happens — a draft sitting unlaunched has consumed nothing. Checking only at
    creation would also mean a campaign scheduled last month could start this month
    without ever being counted against either window.

    Returns True when enforcement is off, whatever the count. The refusal is logged
    and the campaign proceeds, which is what metering-before-blocking means.
    """
    from onboard.constants.entitlement_resource import EntitlementResource
    from onboard.services import entitlements as entitlement_service

    try:
        # requested=0, not 1. This campaign already exists and is already counted
        # against the window — it was counted the moment it was created. Asking for
        # one more would refuse the last campaign of every allowance.
        entitlement_service.check(
            campaign.tenant_id, EntitlementResource.CAMPAIGN_MONTHLY, requested=0
        )
    except entitlement_service.QuotaExceeded as exc:
        logger.warning("campaign %s not started: %s", campaign.pk, exc)
        return False
    except Exception:
        # Fail open, exactly as the credit pre-flight does. A quota lookup must never
        # be the reason a paid-for campaign silently does not run.
        logger.exception(
            "campaign %s: allowance check failed; starting anyway", campaign.pk
        )
    return True


def _has_credit_for_campaign(campaign):
    """Whether this tenant can pay for at least one call on this campaign's bot.

    A deliberately shallow check — one minute at the agent's tier — rather than an
    estimate of the whole audience. Estimating the total would need a guess at average
    call length, and a campaign is not all-or-nothing: it dials until the credits run
    out, and the tenant can top up mid-run. What this catches is the case worth
    catching, which is starting a campaign with an empty or frozen wallet.

    Returns True when enforcement is off, whatever the balance — the refusal is
    logged by the service and the campaign proceeds, which is what metering-before-
    blocking means.
    """
    from wallet.services import policy
    from wallet.services import wallet as wallet_service
    from wallet.services.exceptions import (
        InsufficientCredits,
        RateNotConfigured,
        WalletFrozen,
    )

    agent = getattr(campaign, "agent", None)
    tier = getattr(agent, "service_tier", None) or policy.DEFAULT_TIER

    try:
        wallet_service.assert_can_consume(
            campaign.tenant_id, operation=tier, quantity=60
        )
    except (InsufficientCredits, WalletFrozen) as exc:
        logger.warning(
            "campaign %s not started: %s", campaign.pk, exc
        )
        return False
    except RateNotConfigured:
        # The plan cannot price this bot's tier. Blocking here would take a
        # configuration mistake and turn it into a silently skipped campaign that
        # nobody is told about; the call path raises loudly enough on its own.
        logger.exception(
            "campaign %s: no rate configured for %s; starting anyway",
            campaign.pk, tier,
        )
    except Exception:
        # A credit check must never be why a campaign fails to run.
        logger.exception("campaign %s: credit pre-flight errored; starting anyway", campaign.pk)

    return True


def start_due_campaigns():
    """
    Beat sweep: launch campaigns whose scheduled start has arrived.

    `scheduled_start_at` is a "not before" guarantee, not an exact-fire one — the sweep
    cadence bounds how late a campaign can be.
    """
    from core.tasks import materialize_campaign as materialize_task

    due = Campaign.objects.filter(
        status=Campaign.Status.SCHEDULED,
        scheduled_start_at__lte=timezone.now(),
    ).values_list("pk", flat=True)

    started = 0
    for campaign_id in list(due):
        with transaction.atomic():
            campaign = (
                Campaign.objects.select_for_update().filter(pk=campaign_id).first()
            )
            # Re-check under the lock; another worker may have taken it already.
            if campaign is None or campaign.status != Campaign.Status.SCHEDULED:
                continue

            # Credit pre-flight. Checked before the campaign starts rather than at the
            # first call, because a campaign that dials half its audience and then
            # stops mid-run is worse for the tenant than one that never started: the
            # contacts already called cannot be un-called.
            #
            # While `credits_enforced` is off this only logs, so nothing is blocked
            # until the rates have been proven against real traffic.
            if not _has_credit_for_campaign(campaign):
                continue

            # Plan allowance pre-flight, for the same reason and with the same
            # metering-first behaviour. Starting the campaign is what the allowance
            # counts, so this is the check that decides it.
            if not _within_campaign_allowance(campaign):
                continue

            campaign.status = Campaign.Status.MATERIALIZING
            campaign.save(update_fields=["status", "updated_at"])

        transaction.on_commit(
            lambda cid=campaign_id: materialize_task.delay(str(cid))
        )
        started += 1

    if started:
        logger.info("start_due_campaigns: started %s campaign(s)", started)
    return started
