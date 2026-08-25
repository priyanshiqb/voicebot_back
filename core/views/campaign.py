from onboard.constants.entitlement_resource import EntitlementResource
from tenant.common.views.entitlement_guard import EntitlementGuardMixin
import logging

import django_filters
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.throttling import UserRateThrottle

from core.models.agent import Agent
from core.models.campaign import Campaign
from core.models.campaign_member import CampaignMember
from core.serializers.campaign import (
    CampaignDropdownSerializer,
    CampaignListSerializer,
    CampaignMemberSerializer,
    CampaignSerializer,
    PreviewAudienceSerializer,
)
from core.services import campaigns as campaign_service
from core.tasks import materialize_campaign
from tenant.common.views.agent_tenant_scope import VBAgentTenantScopedMixin
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet

logger = logging.getLogger(__name__)


class CampaignActionThrottle(UserRateThrottle):
    """
    Launch, cancel, and audience preview are the expensive endpoints.

    Preview is included because it counts across the whole selected audience, and an
    under-debounced wizard will call it on every keystroke.
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
    Campaign.Status.SCHEDULED: {"name", "description", "start_date", "start_time"},
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
        if self.action in {"launch", "cancel", "preview_audience"}:
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

    @extend_schema(
        summary="List Campaign Members",
        description="The frozen audience: who is queued, and who was suppressed and why.",
    )
    @action(detail=True, methods=["get"])
    def members(self, request, *args, **kwargs):
        campaign = self._get_campaign()
        queryset = CampaignMember.objects.filter(campaign=campaign).select_related(
            "contact"
        )

        member_status = request.query_params.get("status")
        if member_status:
            queryset = queryset.filter(status=member_status)

        reason = request.query_params.get("suppression_reason")
        if reason:
            queryset = queryset.filter(suppression_reason=reason)

        search = request.query_params.get("search")
        if search:
            queryset = queryset.filter(phone_e164__icontains=search)

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
