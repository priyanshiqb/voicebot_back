"""
Telling the voice runner that a campaign is ready to dial.

This is the campaign's *only* discovery mechanism. `/campaign-runner/` exposes two
detail routes — `context` and `members/update` — both keyed by a campaign id the runner
has no way to learn on its own: there is no list action and no due-work queue. So a
handoff that never lands is not a slow campaign, it is a campaign that silently never
runs. That is why the failure paths here are noisy and the sweeper in `core.tasks`
exists.

What is sent is deliberately thin — ids and an idempotency key. The runner turns around
and pulls `/context/` for the bot config and the audience. Pushing the audience instead
would mean re-implementing pagination over an outbound call and building a second
serialisation path past the suppression filter, which lives in the `/context/` queryset
precisely so there is only one.
"""

import logging

import requests
from django.conf import settings
from django.utils import timezone

from core.models.campaign import Campaign

logger = logging.getLogger(__name__)


class HandoffError(Exception):
    """Base for handoff failures."""


class TransientHandoffError(HandoffError):
    """Worth retrying: a timeout, a 5xx, or explicit backpressure.

    `retry_after` carries a server-supplied delay in seconds when there is one, so a
    runner that tells us how long to wait is believed rather than guessed at.
    """

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentHandoffError(HandoffError):
    """Not worth retrying: a rejected token, a malformed body, a missing URL.

    These fail identically on every attempt, so burning five retries on them only
    delays the moment somebody is told.
    """


def notify_enabled():
    """Whether handoffs should be pushed at all.

    Both halves matter. The flag is the rollout switch; the URL check means a
    half-configured environment refuses loudly at the first campaign instead of
    posting to nowhere.
    """
    return bool(getattr(settings, "CAMPAIGN_RUNNER_NOTIFY_ENABLED", False))


def build_notify_payload(campaign):
    """The notification body.

    `handoff_id` is the contract. It is re-minted on every `/launch/`, so it identifies
    this *attempt* at running this campaign: retries repeat it, a relaunch replaces it.
    The runner is asked to treat a repeat as a duplicate and a new one as superseding.

    `pending_count` is advisory, in the same sense as `credits` on `/context/` — it lets
    the runner size its queue up front. The authoritative list is what `/context/`
    returns, and a runner that ignores this field is not doing anything wrong.
    """
    return {
        "campaign_id": str(campaign.pk),
        "tenant_id": str(campaign.tenant_id),
        "handoff_id": str(campaign.handoff_id),
        "agent_id": str(campaign.agent_id),
        "pending_count": campaign.pending_count,
    }


def _retry_after(response):
    """Seconds from a `Retry-After` header, if it is present and a plain number.

    Only the delay-seconds form is honoured. The HTTP-date form is valid but rare from
    an API, and parsing it wrong would be worse than falling back to our own backoff.
    """
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        seconds = int(raw.strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def notify_runner(campaign):
    """POST the handoff to the runner. Returns True, or raises a HandoffError.

    The 4xx/5xx split is the point of this function: a runner that is down is a
    different problem from a runner that rejected us, and only one of them is worth
    waiting on.
    """
    url = getattr(settings, "CAMPAIGN_RUNNER_NOTIFY_URL", "")
    if not url:
        raise PermanentHandoffError(
            "CAMPAIGN_RUNNER_NOTIFY_URL is not set; the runner cannot be told about "
            "this campaign."
        )

    token = getattr(settings, "CAMPAIGN_RUNNER_TOKEN", "")
    timeout = getattr(settings, "CAMPAIGN_RUNNER_TIMEOUT", 10)
    payload = build_notify_payload(campaign)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        # Duplicated from the body so the runner can dedupe at its edge without
        # parsing JSON.
        "X-Handoff-Id": payload["handoff_id"],
    }

    logger.info(
        "campaign %s: notifying runner at %s (handoff %s, %s pending)",
        campaign.pk,
        url,
        payload["handoff_id"],
        payload["pending_count"],
    )

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise TransientHandoffError(f"could not reach the runner: {exc}") from exc

    status_code = response.status_code

    if 200 <= status_code < 300:
        return True

    # Truncated: an error body is a diagnostic, and a runner returning an HTML error
    # page should not put a kilobyte of markup in every retry's log line.
    detail = (response.text or "")[:200]

    if status_code == 429:
        raise TransientHandoffError(
            f"runner is rate limiting the handoff: {detail}",
            retry_after=_retry_after(response),
        )
    if 400 <= status_code < 500:
        raise PermanentHandoffError(
            f"runner rejected the handoff with {status_code}: {detail}"
        )
    raise TransientHandoffError(f"runner returned {status_code}: {detail}")


def mark_sent(campaign_id):
    """Advance to SENT/RUNNING after a successful notify. Returns True if it ran.

    Both writes are guarded queryset updates rather than `save()`, and both guards
    matter:

    - `handoff_status` only advances from PENDING. The runner may already have called
      `/context/` and set ACKED — it can get there before this task commits — and an
      unguarded write would drag the campaign back to SENT, leaving it permanently
      reading as never-acknowledged.
    - `status` only advances from HANDING_OFF. A tenant may have cancelled during the
      handoff; without the guard this would resurrect a cancelled campaign into RUNNING.

    A queryset update, not `select_for_update` + `save()`: the condition *is* the write,
    so one atomic statement is enough and no lock is held across an HTTP call.
    `updated_at` is set by hand because `auto_now` does not fire on `.update()`.
    """
    now = timezone.now()

    Campaign.objects.filter(
        pk=campaign_id,
        handoff_status=Campaign.HandoffStatus.PENDING,
    ).update(handoff_status=Campaign.HandoffStatus.SENT, updated_at=now)

    started = Campaign.objects.filter(
        pk=campaign_id,
        status=Campaign.Status.HANDING_OFF,
    ).update(status=Campaign.Status.RUNNING, updated_at=now)

    if not started:
        logger.info(
            "campaign %s: notify succeeded but the campaign is no longer handing off; "
            "leaving its status alone",
            campaign_id,
        )
    return bool(started)


def mark_handoff_failed(campaign_id, message):
    """Record a permanently failed handoff without killing the campaign.

    The campaign deliberately stays in HANDING_OFF rather than moving to FAILED.
    HANDING_OFF is in the runner's `WORKABLE_STATUSES`, so a manual replay or an
    operator nudge can still pull `/context/` and work the audience — and the audience
    is already materialised, so failing the campaign would throw away a correct
    snapshot of up to 100k rows over one bad HTTP call. `handoff_status = FAILED` plus
    `error_message` makes it visible, and the sweeper will try again.
    """
    Campaign.objects.filter(pk=campaign_id).update(
        handoff_status=Campaign.HandoffStatus.FAILED,
        error_message=message,
        updated_at=timezone.now(),
    )
    logger.error("campaign %s: handoff failed: %s", campaign_id, message)


def notify_campaign_start(campaign_id):
    """Load the campaign, push it to the runner, and advance it. Raises to signal retry.

    Returns None when there is nothing to do — a campaign that moved on, or a
    notification that has already been acknowledged. Those are ordinary outcomes, not
    failures, so they must not raise: the sweeper re-enqueues this task and a raise
    would turn every stale-sweep no-op into an alert.
    """
    campaign = Campaign.all_objects.filter(pk=campaign_id).first()
    if campaign is None:
        logger.warning("notify_campaign_start: campaign %s not found", campaign_id)
        return None

    if campaign.status != Campaign.Status.HANDING_OFF:
        logger.info(
            "notify_campaign_start: campaign %s is %s, not HANDING_OFF; skipping",
            campaign_id,
            campaign.status,
        )
        return None

    if campaign.handoff_status == Campaign.HandoffStatus.ACKED:
        # The runner pulled /context/ already, so it plainly knows. Re-sending would
        # be a duplicate it has to discard, and re-marking would undo the ACK.
        logger.info(
            "notify_campaign_start: campaign %s already acknowledged; skipping",
            campaign_id,
        )
        return None

    notify_runner(campaign)
    mark_sent(campaign_id)
    return campaign_id


def stale_handoffs():
    """Campaign ids stuck mid-handoff, as a list.

    Catches the cases a task retry cannot: a worker killed while holding the task, an
    `on_commit` hook lost to a restart, and a runner outage that outlasted the retry
    budget.

    The age floor must stay above the notify task's own retry budget — five retries of
    exponential backoff is about fifteen minutes — or the sweeper piles a second task
    on top of one that is still patiently retrying. Re-notifying is safe regardless,
    because `handoff_id` is unchanged and the runner is contracted to treat a repeat as
    a duplicate; the floor is about noise, not correctness.
    """
    from datetime import timedelta

    minutes = getattr(settings, "CAMPAIGN_RUNNER_STALE_HANDOFF_MINUTES", 20)
    cutoff = timezone.now() - timedelta(minutes=minutes)

    return list(
        Campaign.objects.filter(
            status=Campaign.Status.HANDING_OFF,
            handoff_status__in=[
                Campaign.HandoffStatus.PENDING,
                Campaign.HandoffStatus.FAILED,
            ],
            started_at__lte=cutoff,
        ).values_list("pk", flat=True)
    )
