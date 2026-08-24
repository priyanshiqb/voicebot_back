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
from tenant.common.views.agent_tenant_scope import (
    VBAgentActionPermission,
    VBAgentTenantScopedMixin,
)
from user.permissions import IsVoicebotAgent
from utils.views.base_model_viewset import ResponseMixin, SetPagination

logger = logging.getLogger(__name__)

# Campaign states in which the runner is entitled to work the audience.
WORKABLE_STATUSES = [Campaign.Status.RUNNING, Campaign.Status.HANDING_OFF]


@extend_schema(tags=["Campaign Runner"])
class CampaignRunnerViewSet(
    VBAgentTenantScopedMixin, ResponseMixin, viewsets.GenericViewSet
):
    """
    Machine-facing campaign handoff. `VB_AGENT` tokens only.

    The runner pulls a campaign's context, dials, and posts member outcomes back. It
    never writes CampaignMember directly — updates land here so tenant scoping, status
    validation, and counter maintenance stay in one place rather than being trusted to
    an external service.

    SCOPING NOTE — worth understanding before changing anything here. These endpoints
    are scoped to the tenant named in the mandatory `X-Tenant-ID` header; a campaign in
    any other tenant is a 404.

    That header is a filter, not an authorization boundary. `AgentToken` carries no
    tenant, so nothing stops a token holder from naming a different one, and a valid
    runner token can still reach ANY tenant's campaign audience — bulk contact PII —
    by asking for it. Making that a real boundary requires a tenant on AgentToken; the
    header then selects among the tenants that token is allowed, and
    `resolve_agent_tenant` is the only thing that changes. It must not become an `agent`
    id taken from the caller, which is the caller naming its own scope.
    """

    queryset = Campaign.objects.all()
    serializer_class = RunnerCampaignContextSerializer
    permission_classes = [IsAuthenticated, IsVoicebotAgent, VBAgentActionPermission]
    pagination_class = SetPagination

    # Everything this viewset exposes. There is no destroy action and none should be
    # added: a runner reports outcomes, it does not remove campaigns. Listing the
    # actions explicitly means a future @action is denied until deliberately added.
    vb_agent_allowed_actions = frozenset({"context", "update_members"})
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        # Global unless the caller narrows it with `X-Tenant-ID` — see the note above.
        return self.scope_to_agent_tenant(Campaign.objects.filter(is_deleted=False))

    def _get_campaign(self):
        return get_object_or_404(self.get_queryset(), pk=self.kwargs["pk"])

    def _credit_context(self, campaign):
        """What this campaign's calls cost, and what the tenant has left.

        Included so the runner can pace itself and surface a useful message rather
        than discovering an empty wallet one failed call at a time. It is advisory:
        nothing here is load-bearing, and a runner ignoring it is charged exactly the
        same. Never raises — a billing lookup must not be able to break a handoff.
        """
        from wallet.services import policy
        from wallet.services import wallet as wallet_service

        tier = getattr(campaign.agent, "service_tier", None) or policy.DEFAULT_TIER

        try:
            wallet = wallet_service.for_tenant(campaign.tenant_id)
            context = {
                "balance": wallet.balance,
                "is_frozen": wallet.is_frozen,
                "service_tier": tier,
                "credits_per_minute": None,
            }
            try:
                context["credits_per_minute"] = wallet_service.quote(
                    campaign.tenant_id, operation=tier, quantity=60
                ).credits
            except Exception:
                logger.warning(
                    "campaign %s: could not price tier %s", campaign.pk, tier
                )
            return context
        except Exception:
            logger.exception("campaign %s: credit context unavailable", campaign.pk)
            return None

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
            # Additive on purpose: a runner that does not know about this key keeps
            # working unchanged. It is informational — the runner is not asked to
            # enforce anything, because the authoritative refusal happens here and the
            # charge happens when the call log lands.
            "credits": self._credit_context(campaign),
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

        Scoped to the campaign's tenant. `provider_call_id` is globally unique, so this
        is not about colliding ids — it is that the id is supplied by the caller and
        matching on it alone accepted ANY tenant's log. A runner naming another tenant's
        call id had that log stamped with this campaign's member and contact: a
        cross-tenant write chosen entirely by a request field. The tenant filter makes
        the row unreachable instead of merely unlikely to be named.
        """
        if not provider_call_id:
            return
        AgentCallLog.objects.filter(
            provider_call_id=provider_call_id,
            tenant_id=campaign.tenant_id,
        ).update(campaign_member=member, contact=member.contact)
