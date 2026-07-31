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

logger = logging.getLogger(__name__)


def resolve_audience(campaign):
    """
    The contacts this campaign would target, as a queryset.

    Ordered by pk because `.iterator()` over an unordered queryset can repeat or skip
    rows. Soft-deleted contacts are excluded here rather than suppressed, since a
    deleted contact was never really part of the audience.
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

    for contact in contacts.iterator(chunk_size=1000):
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
    for contact in resolve_audience(campaign).iterator(chunk_size=1000):
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
    Freeze the audience into CampaignMember rows and move the campaign to RUNNING.

    Batched with a savepoint per batch rather than one transaction around the whole
    audience: a single bad row must not discard a 100k-member materialisation.
    """
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
        for contact in audience.iterator(chunk_size=batch_size):
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

    campaign.status = Campaign.Status.RUNNING
    campaign.started_at = timezone.now()
    campaign.save(update_fields=["status", "started_at", "updated_at"])
    logger.info(
        "Campaign %s materialized: %s pending, %s suppressed",
        campaign_id,
        campaign.pending_count,
        campaign.suppressed_count,
    )
    return campaign


def _fail(campaign, message):
    campaign.status = Campaign.Status.FAILED
    campaign.error_message = message
    campaign.save(update_fields=["status", "error_message", "updated_at"])
    return campaign


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
            campaign.status = Campaign.Status.MATERIALIZING
            campaign.save(update_fields=["status", "updated_at"])

        transaction.on_commit(
            lambda cid=campaign_id: materialize_task.delay(str(cid))
        )
        started += 1

    if started:
        logger.info("start_due_campaigns: started %s campaign(s)", started)
    return started
