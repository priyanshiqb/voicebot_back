import logging

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated

from core.models.agent_call_log import AgentCallLog
from core.models.campaign import Campaign
from core.models.campaign_member import CampaignMember
from core.serializers.campaign_runner import (
    RunnerCampaignContextSerializer,
    RunnerMemberSerializer,
    RunnerMemberUpdateBatchSerializer,
)
from user.permissions import IsVoicebotAgent
from utils.views.base_model_viewset import ResponseMixin, SetPagination

logger = logging.getLogger(__name__)

# Campaign states in which the runner is entitled to work the audience.
WORKABLE_STATUSES = [Campaign.Status.RUNNING, Campaign.Status.HANDING_OFF]


@extend_schema(tags=["Campaign Runner"])
class CampaignRunnerViewSet(ResponseMixin, viewsets.GenericViewSet):
    """
    Machine-facing campaign handoff. `VB_AGENT` tokens only.

    The runner pulls a campaign's context, dials, and posts member outcomes back. It
    never writes CampaignMember directly — updates land here so tenant scoping, status
    validation, and counter maintenance stay in one place rather than being trusted to
    an external service.

    SCOPING NOTE — deliberate, and worth understanding before changing anything here.
    `AgentToken` carries no tenant, and `VB_AGENT` is a global role by design: it
    already reads every tenant's agents ([agent.py:133]) and call logs
    ([agent_call_log.py:151]). These endpoints follow that same posture, which means a
    valid runner token can read ANY tenant's campaign audience. That audience is bulk
    contact PII, so if per-tenant scoping is ever wanted, the fix is a tenant on
    AgentToken — not a check bolted on here, and not an `agent` id taken from the
    caller, which is the caller naming its own scope.
    """

    queryset = Campaign.objects.all()
    serializer_class = RunnerCampaignContextSerializer
    permission_classes = [IsAuthenticated, IsVoicebotAgent]
    pagination_class = SetPagination

    def get_queryset(self):
        # Not tenant-filtered — see the scoping note above.
        return Campaign.objects.filter(is_deleted=False)

    def _get_campaign(self):
        return get_object_or_404(self.get_queryset(), pk=self.kwargs["pk"])

    @extend_schema(
        summary="Get campaign context",
        description=(
            "The campaign, its bot config, and the members to call. Suppressed members "
            "are never included. Paginated — follow `meta.next` until exhausted."
        ),
    )
    @action(detail=True, methods=["get"])
    def context(self, request, *args, **kwargs):
        campaign = self._get_campaign()

        if campaign.status not in WORKABLE_STATUSES:
            return self.error(
                message=f"Campaign is {campaign.status}; there is nothing to run.",
                status_code=status.HTTP_409_CONFLICT,
            )

        # Filtered in the queryset, not the serializer: suppression is the entire point
        # of this module, and a serializer change must not be able to leak past it.
        members = (
            CampaignMember.objects.filter(
                campaign=campaign,
                status__in=[
                    CampaignMember.Status.PENDING,
                    CampaignMember.Status.IN_PROGRESS,
                ],
            )
            .select_related("contact")
            .order_by("created_at", "pk")
        )

        # First pull is the acknowledgement that the runner has the campaign.
        if campaign.handoff_status != Campaign.HandoffStatus.ACKED:
            campaign.handoff_status = Campaign.HandoffStatus.ACKED
            campaign.handed_off_at = timezone.now()
            campaign.save(
                update_fields=["handoff_status", "handed_off_at", "updated_at"]
            )

        payload = {
            "campaign": RunnerCampaignContextSerializer(campaign).data,
        }

        page = self.paginate_queryset(members)
        if page is not None:
            payload["members"] = RunnerMemberSerializer(page, many=True).data
            response = self.get_paginated_response(payload)
            return response

        payload["members"] = RunnerMemberSerializer(members, many=True).data
        return self.success(data=payload, message="Retrieved successfully")

    @extend_schema(
        summary="Report member outcomes",
        description=(
            "Batch of per-member results. Each is applied independently; the response "
            "reports the outcome of every item. Safe to retry — already-terminal "
            "members are reported as `ignored`, not errors."
        ),
        request=RunnerMemberUpdateBatchSerializer,
    )
    @action(detail=True, methods=["post"], url_path="members/update")
    def update_members(self, request, *args, **kwargs):
        campaign = self._get_campaign()

        serializer = RunnerMemberUpdateBatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updates = serializer.validated_data["updates"]

        results = []
        applied = 0

        for item in updates:
            outcome = self._apply_update(campaign, item)
            results.append(outcome)
            if outcome["result"] == "applied":
                applied += 1

        if applied:
            campaign.recompute_counts()

        return self.success(
            data={
                "applied": applied,
                "total": len(results),
                "results": results,
            },
            message="Updates processed",
        )

    def _apply_update(self, campaign, item):
        member_id = item["member_id"]

        with transaction.atomic():
            member = (
                CampaignMember.objects.select_for_update()
                .filter(pk=member_id, campaign=campaign)
                .first()
            )
            if member is None:
                return {
                    "member_id": str(member_id),
                    "result": "not_found",
                    "detail": "No such member in this campaign.",
                }

            # Terminal is write-once. A retried batch, or two updates arriving out of
            # order, must not move a COMPLETED member backwards or double-count an
            # attempt. Reported as ignored rather than as an error so a runner retrying
            # a whole batch does not see spurious failures.
            if member.is_terminal:
                return {
                    "member_id": str(member_id),
                    "result": "ignored",
                    "detail": f"Member is already {member.status}.",
                }

            new_status = item["status"]
            member.status = new_status
            member.outcome = item.get("outcome") or member.outcome
            member.error_message = item.get("error_message") or ""
            member.last_call_at = item.get("attempted_at") or timezone.now()

            # Counted per reported attempt, not per status change, so a member that
            # goes IN_PROGRESS -> FAILED registers one attempt rather than two.
            if new_status in (
                CampaignMember.Status.COMPLETED,
                CampaignMember.Status.FAILED,
            ):
                member.attempt_count = member.attempt_count + 1

            member.save(
                update_fields=[
                    "status",
                    "outcome",
                    "error_message",
                    "last_call_at",
                    "attempt_count",
                    "updated_at",
                ]
            )

            self._link_call_log(campaign, member, item.get("provider_call_id"))

            if new_status == CampaignMember.Status.COMPLETED:
                # The field has existed on Contact since the contact module shipped and
                # nothing has ever written it. This is where it starts.
                contact = member.contact
                contact.last_contacted_at = member.last_call_at
                contact.save(update_fields=["last_contacted_at", "updated_at"])

        return {"member_id": str(member_id), "result": "applied"}

    @staticmethod
    def _link_call_log(campaign, member, provider_call_id):
        """
        Attach the runner's call log to this member, if it has arrived.

        Linked explicitly rather than matched on contact plus a time window: AgentCallLog
        carries inbound calls too, and a concurrent campaign may be calling the same
        person.
        """
        if not provider_call_id:
            return
        AgentCallLog.objects.filter(provider_call_id=provider_call_id).update(
            campaign_member=member, contact=member.contact
        )
