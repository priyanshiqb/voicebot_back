"""
Tenant seat invitations.

Everything except `accept` requires a manager at the inviting tenant. `accept` is
unauthenticated by necessity — the invitee has no account to authenticate with, which
is the situation the invitation exists to resolve — so it is throttled and its token is
the only credential it accepts.
"""

import logging

from django.conf import settings
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.throttling import AnonRateThrottle

from onboard.constants.entitlement_resource import EntitlementResource
from onboard.serializers.onboarding import GeneralResponseSerializer
from tenant.common.views.entitlement_guard import EntitlementGuardMixin
from tenant.models.TenantInvitation import TenantInvitation
from tenant.services import invitations as invitation_service
from tenant.services import membership
from user.serializers.invitation import (
    InvitationAcceptSerializer,
    InvitationCreateSerializer,
    InvitationListSerializer,
    InvitationSerializer,
)
from user.services import invitation_mail
from utils.views.base_model_viewset import ResponseMixin, SetPagination

logger = logging.getLogger(__name__)


class InvitationAcceptThrottle(AnonRateThrottle):
    """Rate limit on the one unauthenticated write here.

    The token is the only credential, so without this the endpoint is an offer to
    enumerate. Keyed on client IP, which is what an unauthenticated caller has.
    """

    scope = "invitation_accept"


@extend_schema(tags=["Tenant Invitations"])
@extend_schema_view(
    list=extend_schema(
        summary="List invitations",
        description="Outstanding and historical invitations for your organisation.",
    ),
    retrieve=extend_schema(summary="Get an invitation"),
)
class TenantInvitationViewSet(
    EntitlementGuardMixin, ResponseMixin, viewsets.GenericViewSet
):
    entitlement_resource = EntitlementResource.USER

    queryset = TenantInvitation.objects.all()
    serializer_class = InvitationSerializer
    pagination_class = SetPagination
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        # The invitee has no account yet; that is the whole point of the endpoint.
        if self.action == "accept":
            return [AllowAny()]
        return super().get_permissions()

    def get_throttles(self):
        if self.action == "accept":
            return [InvitationAcceptThrottle()]
        return super().get_throttles()

    def get_queryset(self):
        user = self.request.user
        if not user or not user.is_authenticated:
            return TenantInvitation.objects.none()

        mapping = membership.mapping_for(user)
        if mapping is None:
            return TenantInvitation.objects.none()

        return (
            TenantInvitation.objects.filter(
                tenant_id=mapping.tenant_id, is_deleted=False
            )
            .select_related("invited_by", "user")
            .order_by("-created_at")
        )

    def get_serializer_class(self):
        if self.action == "list":
            return InvitationListSerializer
        if self.action == "create":
            return InvitationCreateSerializer
        if self.action == "accept":
            return InvitationAcceptSerializer
        return InvitationSerializer

    def list(self, request, *args, **kwargs):
        # Managers only: a pending invitation names somebody who has not joined yet,
        # and the roster of who is being hired is not ordinary member-visible detail.
        membership.assert_can_manage_members(request.user)

        page = self.paginate_queryset(self.get_queryset())
        serializer = InvitationListSerializer(page, many=True)
        return self.get_paginated_response(serializer.data)

    def retrieve(self, request, *args, **kwargs):
        membership.assert_can_manage_members(request.user)
        return self.success(data=InvitationSerializer(self.get_object()).data)

    @extend_schema(
        summary="Invite somebody",
        request=InvitationCreateSerializer,
        responses={
            202: OpenApiResponse(response=GeneralResponseSerializer, description="Invitation sent, if the address could receive one."),
            402: OpenApiResponse(response=GeneralResponseSerializer, description="No seats left on the plan."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Only an owner or admin may invite."),
        },
    )
    def create(self, request, *args, **kwargs):
        membership.assert_can_manage_members(request.user)

        serializer = InvitationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        mapping = membership.mapping_for(request.user)
        if mapping is None:
            return self.error(
                message="Your user account is not associated with any organisation.",
                status_code=status.HTTP_403_FORBIDDEN,
            )

        invitation, raw_token = invitation_service.create_invitation(
            tenant=mapping.tenant,
            email=serializer.validated_data["email"],
            role=serializer.validated_data["role"],
            invited_by=request.user,
            # Passed as a callable so the entitlement guard — which raises the 402 the
            # frontend already handles — stays owned by the viewset.
            seat_check=lambda: self.check_entitlement(EntitlementResource.USER),
        )

        if invitation is None:
            # The address already belongs to an account. The caller is told nothing
            # that distinguishes this from success: which addresses are registered on
            # this platform is not theirs to discover. Only the mail differs.
            invitation_mail.send_already_registered(
                email=serializer.validated_data["email"],
                tenant=mapping.tenant,
                request=request,
            )
        else:
            invitation_mail.send_invitation(
                invitation=invitation, raw_token=raw_token, request=request
            )

        # 202, not 201: whether a row was created is exactly what must not be leaked.
        return self.success(
            message="If that address can be invited, an invitation is on its way.",
            status_code=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(
        summary="Revoke an invitation",
        request=None,
        responses={
            200: OpenApiResponse(response=GeneralResponseSerializer, description="Invitation revoked and the seat released."),
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Already accepted."),
        },
    )
    @action(detail=True, methods=["post"])
    def revoke(self, request, *args, **kwargs):
        membership.assert_can_manage_members(request.user)

        invitation = self.get_object()
        invitation_service.revoke(invitation, revoked_by=request.user)

        return self.success(message="Invitation revoked")

    @extend_schema(
        summary="Accept an invitation",
        description=(
            "Unauthenticated. Exchanges an invitation token for a usable account by "
            "setting a password. Every failure returns the same message, so this "
            "cannot be used to discover which tokens exist."
        ),
        request=InvitationAcceptSerializer,
        responses={
            200: OpenApiResponse(response=GeneralResponseSerializer, description="Account activated; you may now log in."),
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Invalid or expired token."),
        },
    )
    @action(detail=False, methods=["post"], permission_classes=[AllowAny])
    def accept(self, request, *args, **kwargs):
        serializer = InvitationAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        invitation_service.accept(
            raw_token=serializer.validated_data["token"],
            password=serializer.validated_data["password"],
        )

        return self.success(
            message="Your account is ready. You can now sign in."
        )
