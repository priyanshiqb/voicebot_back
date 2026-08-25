from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import (
    MaxLengthValidator,
    MinLengthValidator,
    RegexValidator,
)
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.models.campaign_member import CampaignMember
from utils.models.base_model_core import BaseQuerySet
from tenant.common.models.tenant_base_model import TenantBaseModel

DEFAULT_SCHEDULE_TIMEZONE = "UTC"


def resolve_tenant_timezone(tenant):
    """
    IANA identifier for a tenant, falling back to UTC.

    Tenant.timezone is nullable and its default is a DB lookup that returns None on an
    unseeded database (onboard.models.timezone.get_default_timezone), so this cannot
    assume the relation is populated.
    """
    from onboard.models.timezone import validate_iana_timezone

    name = getattr(getattr(tenant, "timezone", None), "timezone", None)
    if not name:
        return DEFAULT_SCHEDULE_TIMEZONE
    try:
        # Shared definition of "valid IANA zone" — see onboard.models.timezone.
        validate_iana_timezone(name)
    except ValidationError:
        return DEFAULT_SCHEDULE_TIMEZONE
    return name


def combine_local(start_date, start_time, tz_name):
    """
    Fold a date + time pair into an aware UTC instant.

    Local wall-clock is the only correct frame here: a campaign scheduled for 09:00 must
    fire at 09:00 where the tenant is, not 09:00 UTC.

    DST edges resolve rather than raise — a campaign is not worth failing over a
    one-hour-a-year edge — via the shared policy in `Timezone.to_utc`, which is the
    single conversion authority for the codebase:

      * AMBIGUOUS (fall back): `fold=0`, the first occurrence. Unchanged from the
        previous local implementation.
      * NON-EXISTENT (spring forward): the transition instant, so 02:30 on a
        spring-forward day fires at 03:00. The previous implementation shifted by
        the gap WIDTH and fired at 03:30; the transition instant is nearer the
        scheduler's intent, and one policy across the codebase is worth more than
        preserving a half-hour difference in an edge that occurs once a year.

    The stored UTC instant records exactly which side was chosen either way.
    """
    from onboard.models.timezone import Timezone

    # Unsaved shim when the name is not a seeded row: `zone` resolves from the
    # string alone, so conversion never depends on the seed being present.
    tz_obj = (
        Timezone.objects.filter(timezone=tz_name).first()
        or Timezone(timezone=tz_name)
    )
    utc, _flag = tz_obj.to_utc(start_date, start_time)
    return utc


class Campaign(TenantBaseModel):
    """
    A scheduled outbound calling job. Tier 2 (fully audited + soft delete).

    This model prepares work; it never places calls and never holds a call queue. At the
    scheduled time the audience is frozen into CampaignMember rows, the suppressed ones
    are filtered out, and the whole campaign is handed to the external runner service.
    The runner builds its own queue, dials, paces, and retries — then reports per-member
    updates back, which we reconcile onto CampaignMember.

    Everything here is therefore about *intent, eligibility, and record*: who we mean to
    call, who we must not, and what came back.
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SCHEDULED = "SCHEDULED", "Scheduled"
        MATERIALIZING = "MATERIALIZING", "Freezing audience"
        HANDING_OFF = "HANDING_OFF", "Handing off to runner"
        RUNNING = "RUNNING", "Running at the runner"
        PAUSED = "PAUSED", "Paused"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"
        FAILED = "FAILED", "Failed"

    class AudienceSource(models.TextChoices):
        SELECTED = "SELECTED", "Explicitly selected contacts"
        # Reserved. Shipped in the enum so enabling them later is a serializer change
        # rather than a migration against live campaigns.
        FILTER = "FILTER", "Contacts matching a saved filter"
        UPLOAD = "UPLOAD", "Contacts from an uploaded file"

    class HandoffStatus(models.TextChoices):
        PENDING = "PENDING", "Not yet handed off"
        SENT = "SENT", "Sent to runner"
        ACKED = "ACKED", "Acknowledged by runner"
        FAILED = "FAILED", "Handoff failed"

    # States in which the campaign has not yet been handed over and may still be edited
    # or launched.
    OPEN_STATUSES = [Status.DRAFT, Status.SCHEDULED]
    # States from which no further work happens.
    TERMINAL_STATUSES = [Status.COMPLETED, Status.CANCELLED, Status.FAILED]
    # States where the runner may be acting on our audience right now.
    LIVE_STATUSES = [
        Status.MATERIALIZING,
        Status.HANDING_OFF,
        Status.RUNNING,
        Status.PAUSED,
    ]

    name = models.CharField(
        max_length=100,
        validators=[
            MinLengthValidator(3),
            MaxLengthValidator(100),
            RegexValidator(
                regex=r"^[\w\s-]+$",
                message=(
                    "Campaign Name can only contain letters, numbers, spaces, "
                    "hyphens, and underscores."
                ),
            ),
        ],
        help_text=_("Unique per tenant, case-insensitively."),
    )
    description = models.TextField(
        blank=True,
        validators=[MinLengthValidator(3), MaxLengthValidator(500)],
        help_text=_("Optional. Between 3 and 500 characters when supplied."),
    )
    agent = models.ForeignKey(
        "core.Agent",
        on_delete=models.PROTECT,
        related_name="campaigns",
        help_text=_("The bot that will place these calls."),
    )

    status = models.CharField(
        max_length=15,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
        help_text=_("Server-derived. Never accepted from a client."),
    )

    audience_source = models.CharField(
        max_length=10,
        choices=AudienceSource.choices,
        default=AudienceSource.SELECTED,
    )
    contacts = models.ManyToManyField(
        "core.Contact",
        blank=True,
        related_name="campaigns",
        help_text=_("The audience. Frozen into CampaignMember rows at launch."),
    )
    audience_filter = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Reserved for AudienceSource.FILTER. Unused today."),
    )

    scheduled_start_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text=_("UTC instant derived from the date/time pickers and the tenant timezone."),
    )
    schedule_timezone = models.CharField(
        max_length=64,
        blank=True,
        help_text=_(
            "IANA zone the start was expressed in. Kept so the pickers can be shown "
            "back exactly as typed, and so a later tenant timezone change cannot "
            "silently move an already-scheduled campaign."
        ),
    )

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    launched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="campaigns_launched",
    )
    paused_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="campaigns_paused",
    )
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="campaigns_cancelled",
    )

    # Handoff. Transport-agnostic: these three facts are needed whether the runner is
    # pushed to over HTTP, handed a queue message, or polls for work.
    handoff_id = models.UUIDField(
        null=True,
        blank=True,
        db_index=True,
        help_text=_(
            "Idempotency key, regenerated once per launch and reused across every "
            "delivery attempt of that launch. Lets the runner tell a retry apart from "
            "new work — without it, a delivery that times out after being received "
            "cannot be safely retried."
        ),
    )
    handoff_status = models.CharField(
        max_length=10,
        choices=HandoffStatus.choices,
        default=HandoffStatus.PENDING,
    )
    handed_off_at = models.DateTimeField(null=True, blank=True)

    # Denormalised counters. The detail view is a poll target while a campaign runs, and
    # a GROUP BY over 100k members on every poll is a self-inflicted load test. All are
    # recomputable from CampaignMember, so drift is repairable.
    total_members = models.IntegerField(default=0)
    pending_count = models.IntegerField(default=0)
    suppressed_count = models.IntegerField(default=0)
    completed_count = models.IntegerField(default=0)
    failed_count = models.IntegerField(default=0)

    error_message = models.TextField(blank=True)

    # BaseModelCore's `all_objects` is a plain Manager, so `.delete()` on it is a
    # real DELETE that raises nothing. Campaign rows are the billing record for
    # the campaign allowance — `started_at` is what stops a tenant reclaiming a
    # slot they used — so erasing them in bulk must not be a one-liner. This keeps
    # soft-deleted rows visible, which the allowance count needs, and refuses the
    # hard delete exactly as `objects` already does.
    all_objects = BaseQuerySet.as_manager()

    class Meta:
        # Declaring `all_objects` on the model makes it the newest manager, and
        # Django would otherwise promote it to `_default_manager` -- which the
        # admin, related-object lookups and dumpdata all read through. That would
        # quietly start showing soft-deleted campaigns everywhere. Pin it back.
        default_manager_name = "objects"
        db_table = "core_campaign"
        verbose_name = "Campaign"
        verbose_name_plural = "Campaigns"
        ordering = ["-created_at"]
        constraints = [
            # Case-insensitive: a user who cannot see the difference between
            # "Q1 Leads" and "q1 leads" will report the duplicate as a bug.
            # Partial, so a deleted campaign does not permanently burn its name.
            models.UniqueConstraint(
                Lower("name"),
                "tenant",
                condition=models.Q(is_deleted=False),
                name="unique_active_campaign_name_tenant",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "status"]),
            # The plan-allowance count filters (tenant, created_at >= window
            # start). `tenant, status` gets it into the right tenant and then
            # scans; this covers the window directly, and it runs on every
            # campaign create.
            models.Index(fields=["tenant", "created_at"]),
            models.Index(fields=["status", "scheduled_start_at"]),
        ]

    def __str__(self):
        return self.name

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    @property
    def is_terminal(self):
        return self.status in self.TERMINAL_STATUSES

    @property
    def is_live(self):
        return self.status in self.LIVE_STATUSES

    @property
    def local_start(self):
        """
        `scheduled_start_at` rendered back in the zone it was expressed in.

        Returns None when unscheduled. Used to show the pickers exactly as typed.
        """
        if not self.scheduled_start_at:
            return None
        tz_name = self.schedule_timezone or DEFAULT_SCHEDULE_TIMEZONE
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            tz = ZoneInfo(DEFAULT_SCHEDULE_TIMEZONE)
        return self.scheduled_start_at.astimezone(tz)

    def set_schedule(self, start_date, start_time, tz_name=None):
        """
        Set the start from a date + time pair expressed in the tenant's timezone.

        Both or neither: a date with no time has no defined firing instant, and
        defaulting to midnight is how a system ends up calling people at 00:00.
        """
        if (start_date is None) != (start_time is None):
            raise ValidationError(
                {
                    "start_time": (
                        "Start date and start time must be supplied together."
                    )
                }
            )

        if start_date is None:
            self.scheduled_start_at = None
            self.schedule_timezone = ""
            return

        tz_name = tz_name or resolve_tenant_timezone(self.tenant)
        instant = combine_local(start_date, start_time, tz_name)

        # Compare the combined instant, not the date. "Today at a time two hours ago"
        # is a past start that a date-only check waves through.
        if instant <= timezone.now():
            raise ValidationError(
                {"start_date": "The campaign start must be in the future."}
            )

        self.scheduled_start_at = instant
        self.schedule_timezone = tz_name

    def soft_delete(self, user=None):
        """
        Refuse while the runner may still be working this campaign's audience, and
        refuse permanently once it has been started at all.

        Guarded at the model rather than the viewset because `BaseViewSet.bulk_delete`
        calls `soft_delete()` directly on each object, bypassing `destroy()` entirely.
        A viewset-only check would leave `POST /bulk_delete/` as an open route to
        deleting a live campaign.

        **A campaign that has run is a billing record.** The plan's allowance limits
        the *act* of starting one, and that act is counted from this row — so allowing
        it to be deleted would hand the allowance straight back. A tenant on ten
        campaigns a month could run ten, delete them, and run ten more indefinitely.

        A draft that was never launched consumed nothing and stays deletable, which is
        what keeps the limit about campaigns run rather than campaigns typed.
        """
        if self.status in self.LIVE_STATUSES:
            raise ValidationError(
                "This campaign is still running. Cancel it before deleting."
            )
        if self.started_at is not None:
            raise ValidationError(
                "This campaign has already run and forms part of your billing "
                "record, so it cannot be deleted. You can archive it instead."
            )
        super().soft_delete(user=user)

    def recompute_counts(self, save=True):
        """Rebuild the denormalised counters from CampaignMember. Drift repair."""
        rows = CampaignMember.objects.filter(campaign=self).values("status")
        counts = {
            row["status"]: row["n"]
            for row in rows.annotate(n=models.Count("status")).order_by()
        }

        self.total_members = sum(counts.values())
        self.pending_count = counts.get(CampaignMember.Status.PENDING, 0)
        self.suppressed_count = counts.get(CampaignMember.Status.SUPPRESSED, 0)
        self.completed_count = counts.get(CampaignMember.Status.COMPLETED, 0)
        self.failed_count = counts.get(CampaignMember.Status.FAILED, 0)

        if save:
            self.save(
                update_fields=[
                    "total_members",
                    "pending_count",
                    "suppressed_count",
                    "completed_count",
                    "failed_count",
                ]
            )
