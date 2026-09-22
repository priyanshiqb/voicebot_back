from onboard.constants.entitlement_resource import EntitlementResource
from tenant.common.views.entitlement_guard import EntitlementGuardMixin
import logging

import django_filters
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, OuterRef, Subquery, Value
from django.db.models.functions import Concat, Lower, Trim
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.throttling import UserRateThrottle

from core.models.agent import Agent
from core.models.agent_call_log import AgentCallLog
from core.models.campaign import Campaign
from core.models.campaign_member import CampaignMember
from core.serializers.campaign import (
    CampaignDropdownSerializer,
    CampaignListSerializer,
    CampaignMemberFilterSerializer,
    CampaignMemberSerializer,
    CampaignSerializer,
    PreviewAudienceSerializer,
)
from core.serializers.campaign_report import (
    CampaignReportRequestSerializer,
    CampaignReportSerializer,
)
from core.services import campaign_reports as report_service
from core.services import campaigns as campaign_service
from core.tasks import materialize_campaign
from tenant.common.views.agent_tenant_scope import VBAgentTenantScopedMixin
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from utils.views.ordering import resolve_ordering

logger = logging.getLogger(__name__)


class CampaignActionThrottle(UserRateThrottle):
    """
    Launch, cancel, audience preview and report requests are the expensive endpoints.

    Preview is included because it counts across the whole selected audience, and an
    under-debounced wizard will call it on every keystroke. A report request queues a scan
    of the whole campaign in a worker, and a double-clicked button queues two.
    """

    scope = "campaign_action"

    def get_rate(self):
        return "30/min"


class CampaignFilter(django_filters.FilterSet):
    class Meta:
        model = Campaign
        fields = ["status", "agent", "audience_source", "handoff_status", "is_active"]


# Which fields may change in which state. `None` means everything; a status absent from
# this map allows nothing. Enforced in the viewset rather than the serializer because
# DRF's run_validation re-raises whatever validate() throws as a plain ValidationError,
# which would collapse every state conflict into a 400.
EDITABLE_BY_STATUS = {
    Campaign.Status.DRAFT: None,
    Campaign.Status.SCHEDULED: {
        "name",
        "description",
        "scheduled_start_at",
        "schedule_timezone",
        "start_date",
        "start_time",
    },
    Campaign.Status.RUNNING: {"name", "description"},
    Campaign.Status.PAUSED: {"name", "description"},
}


def blocked_fields(campaign, submitted):
    """Which of the submitted fields this campaign's status forbids changing."""
    allowed = EDITABLE_BY_STATUS.get(campaign.status, set())
    if allowed is None:
        return set()
    return set(submitted) - allowed - {"id"}


@extend_schema(tags=["Campaigns"])
@extend_schema_view(
    list=extend_schema(summary="List Campaigns"),
    retrieve=extend_schema(
        summary="Get Campaign",
        description=(
            "Full detail with live counters. Poll this after launching — "
            "materialisation runs asynchronously and reports through `status`."
        ),
    ),
    create=extend_schema(summary="Create Campaign"),
    update=extend_schema(summary="Update Campaign"),
    partial_update=extend_schema(summary="Partially Update Campaign"),
    destroy=extend_schema(
        summary="Delete Campaign",
        description="Soft delete. Refused with 409 while the campaign is live.",
    ),
)
class CampaignViewSet(EntitlementGuardMixin, VBAgentTenantScopedMixin, TenantBaseViewSet):
    """
    Campaign management.

    A campaign is prepared here and executed elsewhere: at its scheduled time the
    audience is frozen into members and handed to the runner service. `status` is always
    server-derived, and every lifecycle transition is guarded under a row lock so two
    simultaneous requests cannot both win.
    """
    entitlement_resource = EntitlementResource.CAMPAIGN_MONTHLY


    queryset = Campaign.objects.all()
    serializer_class = CampaignSerializer
    list_serializer_class = CampaignListSerializer
    dropdown_serializer_class = CampaignDropdownSerializer

    filterset_class = CampaignFilter
    search_fields = ["name", "description", "agent__bot_name"]
    ordering_fields = [
        "created_at",
        "updated_at",
        "name",
        "scheduled_start_at",
        "status",
    ]
    ordering = ["-created_at"]
    select_related_fields = ["tenant", "agent"]

    # Retrieve only, and the header is mandatory — a machine caller has no tenant
    # mapping, and campaign audiences are bulk contact PII, so there is no version of
    # this that should default to every tenant.
    vb_agent_allowed_actions = frozenset({"retrieve"})

    def get_queryset(self):
        queryset = self.agent_scoped_queryset()
        if queryset is None:
            queryset = super().get_queryset()
        # Prefetched only where it is rendered. Applying it globally would pull an
        # audience of thousands for every campaign on every list page, to populate a
        # field the list serializer does not expose.
        if self.action == "retrieve":
            queryset = queryset.prefetch_related("contacts")
        return queryset

    def get_throttles(self):
        if self.action in {"launch", "cancel", "preview_audience", "reports"}:
            return [CampaignActionThrottle()]
        return super().get_throttles()

    def _get_campaign(self):
        """
        Fetch without running the list filterset.

        DRF's get_object() calls filter_queryset(), so a `?status=` meant for the
        members endpoint would be applied to the campaign lookup and 400 on an
        unrecognised value.
        """
        campaign = get_object_or_404(self.get_queryset(), pk=self.kwargs["pk"])
        self.check_object_permissions(self.request, campaign)
        return campaign

    def _tenant(self):
        try:
            return self.request.user.tenant_mapping.tenant
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Create / update / destroy
    # ------------------------------------------------------------------

    def create(self, request, *args, **kwargs):
        tenant = self._tenant()
        if tenant is not None and not Agent.objects.filter(
            tenant=tenant, is_deleted=False, is_active=True
        ).exists():
            # Machine-readable so the frontend can deep-link to bot creation instead of
            # pattern-matching an English string.
            return self.error(
                message="Create an active bot before creating a campaign.",
                errors={"code": "NO_ACTIVE_BOT"},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        try:
            return super().create(request, *args, **kwargs)
        except IntegrityError:
            # The serializer's uniqueness query is racy by nature; the DB constraint is
            # what actually holds. Without this, two simultaneous creates give one user
            # a 500 instead of a field error.
            return self.error(
                message="A campaign with this name already exists.",
                errors={"name": ["A campaign with this name already exists."]},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        blocked = blocked_fields(instance, request.data or {})
        if blocked:
            # 409, not 400: the request is well-formed, the campaign moved. The UI
            # reaction differs — refresh and retry rather than fix the form.
            return self.error(
                message=(
                    f"A campaign in {instance.status} cannot change: "
                    f"{', '.join(sorted(blocked))}."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
        try:
            return super().update(request, *args, **kwargs)
        except IntegrityError:
            return self.error(
                message="A campaign with this name already exists.",
                errors={"name": ["A campaign with this name already exists."]},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        try:
            self.perform_destroy(instance)
        except DjangoValidationError as exc:
            return self.error(
                message="; ".join(exc.messages),
                status_code=status.HTTP_409_CONFLICT,
            )
        return self.success(message="Deleted successfully")

    @extend_schema(
        summary="Bulk Delete Campaigns",
        description="Soft-deletes several campaigns. Refused with 409 if any is live.",
    )
    @action(detail=False, methods=["post"])
    def bulk_delete(self, request):
        """
        Overridden to surface the live-campaign guard as a clean 409.

        The inherited implementation calls `soft_delete()` inside a transaction and does
        not catch ValidationError, so a live campaign in the batch would surface as a
        500 rather than an explained refusal.
        """
        try:
            return super().bulk_delete(request)
        except DjangoValidationError as exc:
            return self.error(
                message="; ".join(exc.messages),
                status_code=status.HTTP_409_CONFLICT,
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @extend_schema(
        summary="Launch Campaign",
        description=(
            "Validates and starts the campaign. A future start moves it to SCHEDULED "
            "for the beat sweep; otherwise materialisation is enqueued immediately."
        ),
    )
    @action(detail=True, methods=["post"])
    def launch(self, request, *args, **kwargs):
        campaign = self._get_campaign()

        with transaction.atomic():
            locked = (
                Campaign.objects.select_for_update().filter(pk=campaign.pk).first()
            )
            if locked is None:
                return self.error(
                    message="Campaign not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            # The double-click guard: two simultaneous launches, one wins.
            if locked.status != Campaign.Status.DRAFT:
                return self.error(
                    message=f"A campaign in {locked.status} cannot be launched.",
                    status_code=status.HTTP_409_CONFLICT,
                )

            # Re-checked here, not just at create: the bot may have been deleted since.
            agent = locked.agent
            if agent.is_deleted or not agent.is_active:
                return self.error(
                    message="The bot for this campaign is no longer active.",
                    status_code=status.HTTP_409_CONFLICT,
                )

            # Resolved synchronously so a pointless campaign is refused up front,
            # rather than accepted and quietly landing in FAILED minutes later.
            callable_count = campaign_service.count_callable(locked)
            if callable_count == 0:
                return self.error(
                    message=(
                        "This campaign has no contacts that can be called. Everyone "
                        "selected is either suppressed or has no usable phone number."
                    ),
                    status_code=status.HTTP_409_CONFLICT,
                )

            scheduled = locked.scheduled_start_at is not None

            # Only the deferred path reaches start_due_campaigns, where the allowance
            # and credit checks live. A campaign launched for right now goes straight
            # to MATERIALIZING, so without this it would start unchecked. requested=0
            # because this campaign was already counted when it was created; we are
            # asking "are you still inside your allowance", not "may you add one more".
            if not scheduled:
                self.check_entitlement(
                    EntitlementResource.CAMPAIGN_MONTHLY, requested=0
                )

            locked.handoff_id = campaign_service.new_handoff_id()
            locked.handoff_status = Campaign.HandoffStatus.PENDING
            locked.launched_by = request.user
            locked.error_message = ""
            locked.status = (
                Campaign.Status.SCHEDULED
                if scheduled
                else Campaign.Status.MATERIALIZING
            )
            locked.save(
                update_fields=[
                    "status",
                    "handoff_id",
                    "handoff_status",
                    "launched_by",
                    "error_message",
                    "updated_at",
                ]
            )
            campaign_pk = locked.pk
            enqueue = not scheduled

        if enqueue:
            transaction.on_commit(
                lambda: materialize_campaign.delay(str(campaign_pk))
            )

        campaign.refresh_from_db()
        return self.success(
            data=self.get_serializer(campaign).data,
            message="Campaign launched",
            status_code=status.HTTP_202_ACCEPTED,
        )

    def _transition(self, request, expected, new_status, actor_field, message):
        campaign = self._get_campaign()
        with transaction.atomic():
            locked = (
                Campaign.objects.select_for_update().filter(pk=campaign.pk).first()
            )
            if locked is None:
                return self.error(
                    message="Campaign not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            if locked.status not in expected:
                return self.error(
                    message=f"A campaign in {locked.status} cannot be {message}.",
                    status_code=status.HTTP_409_CONFLICT,
                )

            fields = ["status", "updated_at"]
            locked.status = new_status
            if actor_field:
                setattr(locked, actor_field, request.user)
                fields.append(actor_field)
            locked.save(update_fields=fields)

        campaign.refresh_from_db()
        return self.success(
            data=self.get_serializer(campaign).data,
            message=f"Campaign {message}",
        )

    @extend_schema(summary="Pause Campaign")
    @action(detail=True, methods=["post"])
    def pause(self, request, *args, **kwargs):
        return self._transition(
            request,
            expected={Campaign.Status.RUNNING},
            new_status=Campaign.Status.PAUSED,
            actor_field="paused_by",
            message="paused",
        )

    @extend_schema(summary="Resume Campaign")
    @action(detail=True, methods=["post"])
    def resume(self, request, *args, **kwargs):
        return self._transition(
            request,
            expected={Campaign.Status.PAUSED},
            new_status=Campaign.Status.RUNNING,
            actor_field=None,
            message="resumed",
        )

    @extend_schema(
        summary="Cancel Campaign",
        description="Terminal. Remaining pending members are cancelled with it.",
    )
    @action(detail=True, methods=["post"])
    def cancel(self, request, *args, **kwargs):
        campaign = self._get_campaign()

        with transaction.atomic():
            locked = (
                Campaign.objects.select_for_update().filter(pk=campaign.pk).first()
            )
            if locked is None:
                return self.error(
                    message="Campaign not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            if locked.status in Campaign.TERMINAL_STATUSES:
                return self.error(
                    message=f"A campaign in {locked.status} cannot be cancelled.",
                    status_code=status.HTTP_409_CONFLICT,
                )

            # A queryset update, not a Python loop — an audience can be six figures.
            # SUPPRESSED members are deliberately untouched: their reason is a
            # compliance record, not a work item.
            CampaignMember.objects.filter(
                campaign=locked,
                status__in=[
                    CampaignMember.Status.PENDING,
                    CampaignMember.Status.IN_PROGRESS,
                ],
            ).update(status=CampaignMember.Status.CANCELLED)

            locked.status = Campaign.Status.CANCELLED
            locked.cancelled_by = request.user
            locked.save(update_fields=["status", "cancelled_by", "updated_at"])
            locked.recompute_counts()

        campaign.refresh_from_db()
        return self.success(
            data=self.get_serializer(campaign).data, message="Campaign cancelled"
        )

    # ------------------------------------------------------------------
    # Audience
    # ------------------------------------------------------------------

    # Sortable columns on the member table. This action builds its own queryset and
    # never runs filter_queryset, so the viewset's ordering_fields do not reach it —
    # they describe campaigns, not members. `full_name` lives on the joined contact and
    # spans two columns, so it resolves to the annotation added below.
    # `duration_seconds` is the latest call's length, annotated in the action below.
    MEMBER_ORDERING_FIELDS = frozenset(
        {
            "attempt_count",
            "full_name",
            "status",
            "last_call_at",
            "duration_seconds",
            "created_at",
        }
    )
    MEMBER_ORDERING_MAP = {"full_name": "_full_name"}
    MEMBER_ORDERING_DEFAULT = ("created_at",)
    # Members materialised in one transaction share a status, an attempt count and a
    # null last_call_at, so sorting on any of those leaves an audience of thousands in
    # a few enormous ties. Without a total order, page 2 may repeat a row from page 1
    # and drop another — on the endpoint whose whole job is a compliance record.
    MEMBER_ORDERING_TIEBREAKER = ("created_at", "id")
    MEMBER_FULL_NAME_EXPR = Lower(
        Trim(Concat("contact__first_name", Value(" "), "contact__last_name"))
    )

    @extend_schema(
        summary="List Campaign Members",
        description=(
            "The frozen audience: who is queued, and who was suppressed and why.\n\n"
            "Sortable with `?ordering=` on `full_name`, `status`, `attempt_count`, "
            "`last_call_at`, `duration_seconds` or `created_at`; prefix with `-` for "
            "descending."
        ),
        parameters=[
            OpenApiParameter(
                name="ordering",
                required=False,
                type=str,
                description=(
                    "full_name, status, attempt_count, last_call_at, duration_seconds, "
                    "created_at. Prefix with '-' for descending. Supports arrays (e.g. "
                    "`ordering[]=-status`) and fallback names `sort` and `sortBy`."
                ),
            ),
            OpenApiParameter(
                name="min_duration",
                required=False,
                type=int,
                description=(
                    "Latest call at least this many seconds long, inclusive. Leaves out "
                    "members with no linked call. 400 if not a whole number >= 0."
                ),
            ),
            OpenApiParameter(
                name="max_duration",
                required=False,
                type=int,
                description=(
                    "Latest call at most this many seconds long, inclusive. Leaves out "
                    "members with no linked call. 400 if not a whole number >= 0, or if "
                    "below min_duration."
                ),
            ),
            OpenApiParameter(
                name="status",
                required=False,
                type=str,
                description=(
                    "Filter by member status: one or more of PENDING, SUPPRESSED, CANCELLED, "
                    "IN_PROGRESS, COMPLETED, FAILED. Comma-separated values or multiple query "
                    "parameters for multi-select; a member matching any of them is returned."
                ),
            ),
            OpenApiParameter(
                name="suppression_reason",
                required=False,
                type=str,
                description="Filter by suppression reason.",
            ),
            OpenApiParameter(
                name="search",
                required=False,
                type=str,
                description="Search by phone number.",
            ),
            OpenApiParameter(
                name="outcome",
                required=False,
                type=str,
                description=(
                    "Filter by exact call outcome. Supports comma-separated values "
                    "or multiple query parameters for multi-select."
                ),
            ),
            OpenApiParameter(
                name="start_date",
                required=False,
                type=str,
                description="Filter by last_call_at greater than or equal to this date (ISO 8601).",
            ),
            OpenApiParameter(
                name="end_date",
                required=False,
                type=str,
                description="Filter by last_call_at less than or equal to this date (ISO 8601).",
            ),
        ],
    )
    @action(detail=True, methods=["get"])
    def members(self, request, *args, **kwargs):
        campaign = self._get_campaign()
        # The member's newest call log, so a row can link to its call analytics
        # (GET /agent-call-logs/{id}/). Newest by created_at, the same "latest call" the
        # campaign report reads, so the report and this screen name the same call.
        # Soft-deleted logs are skipped because their detail endpoint 404s. A subquery
        # rather than a prefetch keeps it to the one query per page.
        latest_call = AgentCallLog.objects.filter(
            campaign_member=OuterRef("pk"), is_deleted=False
        ).order_by("-created_at")
        queryset = (
            CampaignMember.objects.filter(campaign=campaign)
            .select_related("contact")
            .annotate(
                _full_name=self.MEMBER_FULL_NAME_EXPR,
                analytics_id=Subquery(latest_call.values("id")[:1]),
                # The same call's length, so the row, the duration filters and the
                # duration sort all describe the call analytics_id opens.
                duration_seconds=Subquery(latest_call.values("duration_seconds")[:1]),
            )
        )

        # Checked before use: a non-integer bound or an upside-down range is a 400, not
        # a filter that silently matches nobody.
        member_filters = CampaignMemberFilterSerializer(data=request.query_params)
        member_filters.is_valid(raise_exception=True)
        # A member with no linked call has a null duration, which neither bound matches,
        # so setting either one leaves never-called members out.
        min_duration = member_filters.validated_data.get("min_duration")
        if min_duration is not None:
            queryset = queryset.filter(duration_seconds__gte=min_duration)
        max_duration = member_filters.validated_data.get("max_duration")
        if max_duration is not None:
            queryset = queryset.filter(duration_seconds__lte=max_duration)

        # One status or several: `?status=PENDING,IN_PROGRESS`, or the key repeated.
        # Not `get()`: on a repeated key it keeps only the last value, which silently
        # narrowed a multi-select to whichever status happened to be sent last.
        statuses = [
            value.strip()
            for param in request.query_params.getlist("status")
            for value in param.split(",")
            if value.strip()
        ]
        if statuses:
            queryset = queryset.filter(status__in=statuses)

        reason = request.query_params.get("suppression_reason")
        if reason:
            queryset = queryset.filter(suppression_reason=reason)

        search = request.query_params.get("search")
        if search:
            queryset = queryset.filter(phone_e164__icontains=search)

        outcomes = request.query_params.getlist("outcome")
        if outcomes:
            if len(outcomes) == 1 and "," in outcomes[0]:
                outcomes = [x.strip() for x in outcomes[0].split(",")]
            queryset = queryset.filter(outcome__in=outcomes)



        start_date = request.query_params.get("start_date")
        if start_date:
            queryset = queryset.filter(last_call_at__gte=start_date)

        end_date = request.query_params.get("end_date")
        if end_date:
            queryset = queryset.filter(last_call_at__lte=end_date)

        queryset = queryset.order_by(
            *resolve_ordering(
                request,
                allowed=self.MEMBER_ORDERING_FIELDS,
                default=self.MEMBER_ORDERING_DEFAULT,
                field_map=self.MEMBER_ORDERING_MAP,
                tiebreaker=self.MEMBER_ORDERING_TIEBREAKER,
            )
        )

        page = self.paginate_queryset(queryset)
        if page is not None:
            return self.get_paginated_response(
                CampaignMemberSerializer(page, many=True).data
            )
        return self.success(
            data=CampaignMemberSerializer(queryset, many=True).data,
            message="Listed successfully",
        )

    @extend_schema(
        summary="Campaign Stats",
        description="Counters and the suppression breakdown, without paging members.",
    )
    @action(detail=True, methods=["get"])
    def stats(self, request, *args, **kwargs):
        campaign = self._get_campaign()

        by_status = {
            row["status"]: row["n"]
            for row in CampaignMember.objects.filter(campaign=campaign)
            .values("status")
            .annotate(n=Count("status"))
            .order_by()
        }
        by_reason = {
            row["suppression_reason"]: row["n"]
            for row in CampaignMember.objects.filter(campaign=campaign)
            .exclude(suppression_reason="")
            .values("suppression_reason")
            .annotate(n=Count("suppression_reason"))
            .order_by()
        }

        return self.success(
            data={
                "campaign_id": str(campaign.id),
                "status": campaign.status,
                "total_members": campaign.total_members,
                "pending_count": campaign.pending_count,
                "suppressed_count": campaign.suppressed_count,
                "completed_count": campaign.completed_count,
                "failed_count": campaign.failed_count,
                "by_status": by_status,
                "suppressed_by_reason": by_reason,
            },
            message="Retrieved successfully",
        )

    @extend_schema(
        summary="Request a Campaign Report",
        description=(
            "Queues an XLSX or PDF report on this campaign and returns **202** at once with "
            "the report row. The file is built in the background: poll "
            "`GET /campaign-reports/{id}/` until `status` is `COMPLETED`, then call its "
            "`download` action. The requester also gets an in-app notification."
        ),
        request=CampaignReportRequestSerializer,
        responses={
            202: CampaignReportSerializer,
            400: OpenApiResponse(description="Unknown `report_format`."),
            404: OpenApiResponse(description="No such campaign in your organisation."),
        },
    )
    @action(detail=True, methods=["post"], url_path="reports")
    def reports(self, request, *args, **kwargs):
        """Nothing expensive happens here: one row is written and a task is scheduled.

        Allowed in any campaign status. A draft's report is mostly empty, which is itself a
        true answer, and refusing it would make the button depend on state the user may not
        be looking at.
        """
        campaign = self._get_campaign()
        serializer = CampaignReportRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        report = report_service.request_report(
            campaign, request.user, serializer.validated_data["report_format"]
        )
        return self.success(
            data=CampaignReportSerializer(
                report, context=self.get_serializer_context()
            ).data,
            message="Report requested",
            status_code=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(
        summary="Preview Audience",
        description=(
            "How many of these contacts can actually be called, and why the rest "
            "cannot. Detail-free so the wizard can call it before the campaign exists."
        ),
        request=PreviewAudienceSerializer,
    )
    @action(detail=False, methods=["post"], url_path="preview-audience")
    def preview_audience(self, request, *args, **kwargs):
        serializer = PreviewAudienceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        tenant = self._tenant()
        if tenant is None:
            return self.error(
                message="Your user account is not associated with any active tenant.",
                status_code=status.HTTP_403_FORBIDDEN,
            )

        return self.success(
            data=campaign_service.preview_audience(
                tenant, serializer.validated_data["contact_ids"]
            ),
            message="Retrieved successfully",
        )
