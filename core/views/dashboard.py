"""
The tenant dashboard, in one request.

Deliberately **live queries, no rollups and no metric registry** — the full analytics
module in `docs/dashboard/modules/06-analytics.md` is a different, much larger piece of
work. This serves the numbers the existing models can already answer honestly, today.
At current volumes aggregation costs nothing; rollups become worth building when a
tenant's call table is large enough for these queries to be felt, and not before.

**One endpoint, not nine.** Every widget on the page is filtered by the same date range,
so nine endpoints would mean nine round trips that must agree with each other about what
"this month" means. They would drift the first time one of them rounded a boundary
differently.

**What is deliberately absent.** There is no success rate, no conversion rate and no
resolution rate. All three need `outcome` to be classifiable, and `outcome` is free text
on both AgentCallLog and CampaignMember — "Stored verbatim; no fixed vocabulary". A
number invented from an unclassified string would be worse than no number: it would look
authoritative and mean nothing. What is returned instead is `connect_rate`, which is
derivable from `duration_seconds` and actually true, plus `outcome_breakdown`, which is
the raw material for whatever success is eventually defined to be.
"""

import logging
from datetime import datetime, timedelta

from django.db.models import Avg, Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_date
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    OpenApiTypes,
    extend_schema,
)
from rest_framework import status as http_status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated

from core.models.agent import Agent
from core.models.agent_call_log import AgentCallLog
from core.models.booking import Booking
from core.models.booking_participant import BookingParticipant
from core.models.campaign import Campaign
from utils.views.base_model_viewset import ResponseMixin

logger = logging.getLogger(__name__)

#: Default window when the caller gives no range. Seven days, so the volume chart has
#: seven columns without the client having to ask for them.
DEFAULT_DAYS = 7

#: Ceiling on the requested range. A tenant asking for five years of daily buckets would
#: get 1,825 rows and a slow scan; this fails loudly instead of quietly taking a minute.
MAX_DAYS = 366

#: How many rows the list widgets return. They are previews with a "View All" behind
#: them, not paginated tables.
PREVIEW_LIMIT = 5
CAMPAIGN_LIMIT = 5
LEADERBOARD_LIMIT = 10


def _pct_change(current, previous):
    """Percentage movement between two windows, or None when there is no baseline.

    ``None`` rather than 0 or 100 when the previous window is empty: "no change" and
    "nothing to compare against" are different statements, and a dashboard that renders
    +0.0% for a tenant's first week is asserting something it does not know.
    """
    if not previous:
        return None
    return round(((current - previous) / previous) * 100, 1)


class DashboardViewSet(ResponseMixin, viewsets.ViewSet):
    """Read-only tenant dashboard. Everything is scoped to the caller's own tenant.

    A plain ``ViewSet``, not ``GenericViewSet``: there is no queryset and no model
    behind this — the response is assembled from eight different ones — so a
    ``serializer_class`` would be a fiction. GenericViewSet insists on one and
    schema generation drops the whole view when it cannot find it.
    """

    permission_classes = [IsAuthenticated]

    def _tenant(self):
        try:
            return self.request.user.tenant_mapping.tenant
        except Exception:
            return None

    def _range(self, request):
        """The requested window, plus the equally-long window before it.

        The previous window is what every `delta_pct` is measured against, and it is
        derived here rather than by the client so the two can never disagree about how
        long "the same period last time" is.

        Filtering is on ``created_at``, not ``started_at``. ``started_at`` is
        nullable, and a range filter on a nullable column silently excludes every row
        that lacks it — a call log posted without a start time would vanish from every
        total rather than being counted late. ``created_at`` is always present, so the
        numbers add up to the number of calls that exist.

        The trade is that a log posted well after the call ends is filed on the day it
        arrived rather than the day it happened. That is visible and explainable;
        silently missing calls are neither.
        """
        now = timezone.now()
        raw_to = request.query_params.get("to")
        raw_from = request.query_params.get("from")

        end_date = parse_date(raw_to) if raw_to else None
        start_date = parse_date(raw_from) if raw_from else None

        if raw_to and end_date is None:
            raise ValueError("`to` must be a date, as YYYY-MM-DD.")
        if raw_from and start_date is None:
            raise ValueError("`from` must be a date, as YYYY-MM-DD.")

        end = (
            timezone.make_aware(
                datetime.combine(end_date, datetime.max.time())
            )
            if end_date
            else now
        )
        start = (
            timezone.make_aware(
                datetime.combine(start_date, datetime.min.time())
            )
            if start_date
            else end - timedelta(days=DEFAULT_DAYS - 1)
        )

        if start > end:
            raise ValueError("`from` cannot be after `to`.")

        span = end - start
        if span > timedelta(days=MAX_DAYS):
            raise ValueError(f"Range too wide — {MAX_DAYS} days maximum.")

        return start, end, start - span, start

    # ------------------------------------------------------------------

    @extend_schema(
        summary="Tenant dashboard",
        description=(
            "Every widget on the dashboard in one response, for one date range.\n\n"
            "Defaults to the last 7 days. `delta_pct` compares against the "
            "immediately preceding window of the same length, and is `null` when that "
            "window had nothing to compare against.\n\n"
            "**No success/conversion/resolution rate is returned.** Those require "
            "`outcome` to be classifiable and it is free text; use "
            "`outcome_breakdown` and decide the vocabulary first."
        ),
        parameters=[
            OpenApiParameter("from", OpenApiTypes.DATE, description="Inclusive start, YYYY-MM-DD."),
            OpenApiParameter("to", OpenApiTypes.DATE, description="Inclusive end, YYYY-MM-DD."),
        ],
        # Described as a free-form object rather than a serializer: the payload is
        # eight unrelated blocks assembled by hand, and inventing eight serializers
        # that no code path uses would be schema theatre. The shape is documented
        # in docs/apiDocs/analytics/dashboard.md.
        responses={
            200: OpenApiResponse(OpenApiTypes.OBJECT, description="The dashboard payload."),
            400: OpenApiResponse(OpenApiTypes.OBJECT, description="Malformed or oversized date range."),
            403: OpenApiResponse(OpenApiTypes.OBJECT, description="Caller has no tenant."),
        },
    )
    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request):
        tenant = self._tenant()
        if tenant is None:
            return self.error(
                message="Your user account is not associated with any organisation.",
                status_code=http_status.HTTP_403_FORBIDDEN,
            )

        try:
            start, end, prev_start, prev_end = self._range(request)
        except ValueError as exc:
            return self.error(
                message=str(exc), status_code=http_status.HTTP_400_BAD_REQUEST
            )

        calls = AgentCallLog.objects.filter(
            tenant=tenant, created_at__gte=start, created_at__lte=end
        )
        previous = AgentCallLog.objects.filter(
            tenant=tenant, created_at__gte=prev_start, created_at__lt=prev_end
        )

        return self.success(
            data={
                "range": {
                    "from": start.isoformat(),
                    "to": end.isoformat(),
                    "previous_from": prev_start.isoformat(),
                    "previous_to": prev_end.isoformat(),
                },
                "totals": self._totals(tenant, calls, previous),
                "call_volume": self._call_volume(calls, start, end),
                "outcome_breakdown": self._outcome_breakdown(calls),
                "campaigns": self._campaigns(tenant),
                "recent_calls": self._recent_calls(tenant),
                "agent_leaderboard": self._leaderboard(calls),
                "bookings": self._bookings(tenant),
            },
            message="Retrieved successfully",
        )

    # ------------------------------------------------------------------
    # Widgets
    # ------------------------------------------------------------------

    def _totals(self, tenant, calls, previous):
        """The stat tiles.

        `connect_rate` stands in for the "success rate" the design asks for. It is the
        share of calls that produced any talk time at all, which is a real property of
        the data. A success rate is not, until somebody defines which of
        "callback requested", "information provided" and "no action" count as success.
        """
        def _shape(queryset):
            row = queryset.aggregate(
                total=Count("id"),
                connected=Count("id", filter=Q(duration_seconds__gt=0)),
                avg_duration=Avg("duration_seconds", filter=Q(duration_seconds__gt=0)),
            )
            row["connect_rate"] = (
                round((row["connected"] / row["total"]) * 100, 1)
                if row["total"]
                else None
            )
            row["avg_duration"] = (
                round(row["avg_duration"]) if row["avg_duration"] else 0
            )
            return row

        now = _shape(calls)
        was = _shape(previous)

        from tenant.models.TenantUserMapping import TenantUserMapping

        return {
            "total_calls": {
                "value": now["total"],
                "delta_pct": _pct_change(now["total"], was["total"]),
            },
            # The design labels this "Avg Response". This is average talk time on
            # connected calls — nothing records time-to-answer, so if that is what was
            # meant, it needs a new field on the call log first.
            "avg_duration_seconds": {
                "value": now["avg_duration"],
                "delta_pct": _pct_change(now["avg_duration"], was["avg_duration"]),
            },
            "connect_rate": {
                "value": now["connect_rate"],
                "delta_pct": _pct_change(
                    now["connect_rate"] or 0, was["connect_rate"] or 0
                ),
            },
            # Two different things the design conflates as "Active Seats (n agents)":
            # seats are people who can log in, agents are bots. Both returned so the
            # client can label them correctly.
            "active_agents": {
                "value": Agent.objects.filter(
                    tenant=tenant, is_deleted=False, is_active=True
                ).count()
            },
            "active_seats": {
                "value": TenantUserMapping.objects.filter(
                    tenant=tenant, is_deleted=False
                ).count()
            },
        }

    def _call_volume(self, calls, start, end):
        """Inbound and outbound per day, with empty days filled in.

        Filled rather than sparse: a chart that silently omits a zero day compresses
        the x-axis and makes a quiet Sunday look like it never happened.
        """
        rows = (
            calls.values("created_at__date", "direction")
            .annotate(count=Count("id"))
            .order_by()
        )

        buckets = {}
        for row in rows:
            day = row["created_at__date"]
            entry = buckets.setdefault(day, {"inbound": 0, "outbound": 0})
            if row["direction"] == AgentCallLog.CallDirection.INBOUND:
                entry["inbound"] += row["count"]
            else:
                entry["outbound"] += row["count"]

        series = []
        cursor = start.date()
        last = end.date()
        while cursor <= last:
            entry = buckets.get(cursor, {"inbound": 0, "outbound": 0})
            series.append({
                "date": cursor.isoformat(),
                "inbound": entry["inbound"],
                "outbound": entry["outbound"],
                "total": entry["inbound"] + entry["outbound"],
            })
            cursor += timedelta(days=1)
        return series

    def _outcome_breakdown(self, calls):
        """Counts per disposition, highest first.

        The honest substitute for a success rate: it reports what the runner actually
        said without pretending to know which of those outcomes is a win.
        """
        rows = (
            calls.exclude(Q(outcome__isnull=True) | Q(outcome=""))
            .values("outcome")
            .annotate(count=Count("id"))
            .order_by("-count")
        )
        return [{"outcome": r["outcome"], "count": r["count"]} for r in rows]

    def _campaigns(self, tenant):
        """Live campaigns and their progress.

        Progress comes from the denormalised counters the campaign already maintains,
        so this is one query and no per-campaign member scan.

        No conversion rate: that needs CampaignMember.outcome classified, and it is
        free text for the same reason the call outcome is.
        """
        campaigns = (
            Campaign.objects.filter(tenant=tenant, is_deleted=False)
            .exclude(status=Campaign.Status.DRAFT)
            .order_by("-created_at")[:CAMPAIGN_LIMIT]
        )

        out = []
        for campaign in campaigns:
            dialed = campaign.completed_count + campaign.failed_count
            # Suppressed members are excluded from the denominator: they were never
            # going to be dialled, so counting them would make a fully-worked campaign
            # read as permanently incomplete.
            dialable = campaign.total_members - campaign.suppressed_count
            out.append({
                "id": str(campaign.id),
                "name": campaign.name,
                "status": campaign.status,
                "dialed": dialed,
                "dialable": dialable,
                "total_members": campaign.total_members,
                "pending": campaign.pending_count,
                "suppressed": campaign.suppressed_count,
                "completed": campaign.completed_count,
                "failed": campaign.failed_count,
                "progress_pct": (
                    round((dialed / dialable) * 100, 1) if dialable else 0.0
                ),
            })
        return out

    def _recent_calls(self, tenant):
        """The live stream. Not range-filtered — "recent" means recent.

        `customer_number` rather than a raw column: the human is the caller on inbound
        and the callee on outbound, and the model property is the only thing that knows
        which, including for older rows written before callee_number existed.
        """
        calls = (
            AgentCallLog.objects.filter(tenant=tenant)
            .select_related("agent")
            # created_at, not started_at: it is non-null, so nothing sorts oddly
            # and the (tenant, -created_at) index serves this directly.
            .order_by("-created_at")[:PREVIEW_LIMIT]
        )
        return [
            {
                "id": str(call.id),
                "customer_number": call.customer_number,
                "agent_name": call.agent.bot_name if call.agent_id else None,
                "direction": call.direction,
                "outcome": call.outcome or None,
                "duration_seconds": call.duration_seconds,
                "started_at": call.started_at.isoformat() if call.started_at else None,
            }
            for call in calls
        ]

    def _leaderboard(self, calls):
        """Per-bot volume and connect rate, busiest first.

        Ranked on volume, not on a resolution rate — see the module docstring. The
        design's "Standby" badge has nothing behind it either: Agent has no status
        field, only is_active.
        """
        rows = (
            calls.filter(agent__isnull=False)
            .values("agent_id", "agent__bot_name")
            .annotate(
                total=Count("id"),
                connected=Count("id", filter=Q(duration_seconds__gt=0)),
                avg_duration=Avg("duration_seconds", filter=Q(duration_seconds__gt=0)),
            )
            .order_by("-total")[:LEADERBOARD_LIMIT]
        )
        return [
            {
                "agent_id": str(r["agent_id"]),
                "agent_name": r["agent__bot_name"],
                "calls": r["total"],
                "connect_rate": (
                    round((r["connected"] / r["total"]) * 100, 1) if r["total"] else None
                ),
                "avg_duration_seconds": (
                    round(r["avg_duration"]) if r["avg_duration"] else 0
                ),
            }
            for r in rows
        ]

    def _bookings(self, tenant):
        """Upcoming appointments booked by a bot.

        `source=AI` is what marks a booking as bot-made. Which *bot* made it is not
        recorded anywhere — Booking has no FK to Agent — so `host_name` below is the
        person the appointment is with, not the bot that booked it. The design's
        "Booked via Dr. Priya" reads as the latter; if that is what is wanted, Booking
        needs an agent field.
        """
        bookings = (
            Booking.objects.filter(
                tenant=tenant,
                is_deleted=False,
                source=Booking.Source.AI,
                start_time__gte=timezone.now(),
            )
            .select_related("primary_host")
            .prefetch_related("participants")
            .order_by("start_time")[:PREVIEW_LIMIT]
        )

        out = []
        for booking in bookings:
            # First non-host participant: the person the appointment is for. Iterated
            # in Python rather than filtered in SQL so the prefetch above is actually
            # used — a .filter() here would issue one extra query per booking.
            attendee = next(
                (p for p in booking.participants.all()
                 if p.role != BookingParticipant.Role.HOST),
                None,
            )
            host = booking.primary_host
            out.append({
                "id": str(booking.id),
                "title": booking.title,
                "attendee_name": attendee.name if attendee else None,
                "host_name": (
                    f"{host.first_name} {host.last_name}".strip() or host.email
                    if host else None
                ),
                "status": booking.status,
                "start_time": booking.start_time.isoformat(),
            })
        return out
