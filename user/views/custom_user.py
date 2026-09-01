from onboard.constants.entitlement_resource import EntitlementResource
from tenant.common.views.entitlement_guard import EntitlementGuardMixin
from tenant.services import membership
from user.serializers.custom_user import (
    UserSerializer,
    UserListSerializer,
    UserDropdownSerializer,
    ChangePasswordSerializer,
    SetRoleSerializer,
)
from user.models.CustomUser import CustomUser
from django.db import models
from utils.views.base_model_viewset import BaseViewSet
from drf_spectacular.utils import (
    extend_schema,
    extend_schema_view,
    OpenApiParameter,
    OpenApiTypes,
    OpenApiResponse,
)
from onboard.serializers.onboarding import GeneralResponseSerializer
from rest_framework import status
from rest_framework.decorators import action


@extend_schema(tags=["Users"])
@extend_schema_view(
    list=extend_schema(
        summary="List Users",
        description="Get paginated list of users.",
        parameters=[
            OpenApiParameter(
                name="is_active",
                type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
            ),
            OpenApiParameter(
                name="search",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
            ),
        ],
        responses={
            200: UserListSerializer(many=True),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied.")
        }
    ),
    create=extend_schema(
        summary="Create User",
        description="Create a new user.",
        responses={
            201: UserSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied.")
        }
    ),
    retrieve=extend_schema(
        summary="Get User",
        description="Get specific user details by ID.",
        responses={
            200: UserSerializer,
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="User not found.")
        }
    ),
    update=extend_schema(
        summary="Update User",
        description="Completely update an existing user by ID.",
        responses={
            200: UserSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="User not found.")
        }
    ),
    partial_update=extend_schema(
        summary="Partial Update User",
        description="Partially update an existing user by ID.",
        responses={
            200: UserSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="User not found.")
        }
    ),
    destroy=extend_schema(
        summary="Delete User",
        description="Soft-delete an existing user by ID.",
        responses={
            200: OpenApiResponse(response=GeneralResponseSerializer, description="User deleted successfully."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="User not found.")
        }
    ),
    me=extend_schema(
        summary="Get Current User",
        description="Get details of the currently authenticated user.",
        responses={
            200: UserSerializer,
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request.")
        }
    )
)
class CustomUserViewSet(EntitlementGuardMixin, BaseViewSet):

    entitlement_resource = EntitlementResource.USER

    queryset = CustomUser.objects.all()

    serializer_class = UserSerializer
    list_serializer_class = UserListSerializer
    dropdown_serializer_class = UserDropdownSerializer

    select_related_fields = [
        "created_by",
        "updated_by",
        "deleted_by",
        "tenant_mapping",
        "tenant_mapping__tenant",
    ]

    prefetch_related_fields = [
        "tenant_mapping__tenant__subscriptions",
        "tenant_mapping__tenant__subscriptions__plan",
    ]

    search_fields = [
        "email",
        "username",
        "first_name",
        "last_name",
        "phone_number",
    ]

    ordering_fields = [
        "created_at",
        "updated_at",
        "email",
        "first_name",
        "last_name",
    ]

    filterset_fields = {
        "is_active": ["exact"],
        "is_verified": ["exact"],
        "created_at": ["date", "gte", "lte"],
    }

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user

        if not user or not user.is_authenticated:
            return queryset.none()

        if user.is_superuser:
            return queryset

        # Standard users can see and edit their own record
        q_filter = models.Q(id=user.id)

        try:
            # They can also see and edit other users under the same tenant mapping
            active_tenant = user.tenant_mapping.tenant
            q_filter |= models.Q(tenant_mapping__tenant=active_tenant)
        except Exception:
            pass

        return queryset.filter(q_filter)

    def perform_create(self, serializer, **kwargs):
        # Permission before quota, deliberately. A member who may not add people at all
        # should be told that — not handed a 402 that discloses how much of the
        # organisation's plan is used up.
        membership.assert_can_manage_members(self.request.user)

        # Called explicitly: this method overrides the mixin's rather than extending
        # it, so the seat check would otherwise never run. Before the mapping is
        # created, not after — a user who has already been added is a user you then
        # have to remove.
        self.check_entitlement(EntitlementResource.USER)

        user = self.request.user
        from django.db import transaction

        with transaction.atomic():
            new_user = serializer.save()

            if not user.is_superuser:
                try:
                    active_tenant = user.tenant_mapping.tenant
                    from tenant.models.TenantUserMapping import TenantUserMapping
                    TenantUserMapping.objects.create(
                        user=new_user,
                        tenant=active_tenant,
                        # Least authority on arrival. Promotion is a separate,
                        # deliberate act through `set-role`, so nobody is created able
                        # to remove the person who created them.
                        role=TenantUserMapping.Role.MEMBER,
                        created_by=user,
                        updated_by=user
                    )
                except Exception:
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("Your user account is not associated with any active tenant.")

    def perform_update(self, serializer):
        # `get_queryset` widens to the whole tenant so colleagues are visible and
        # maintainable; this is what decides who may actually write. Without it any
        # member could deactivate any colleague by setting `is_active`.
        membership.assert_can_act_on(self.request.user, serializer.instance)
        super().perform_update(serializer)

    def perform_destroy(self, instance):
        membership.assert_can_act_on(self.request.user, instance)

        from django.db import transaction

        with transaction.atomic():
            # Release the seat in the same transaction as the removal. The mapping is
            # what the USER allowance counts, and its FK cascade never fires here
            # because a soft delete is a flag write — see membership.release_seat.
            membership.release_seat(instance, deleted_by=self.request.user)
            super().perform_destroy(instance)

    @extend_schema(
        summary="Bulk Delete Users",
        description=(
            "Remove several users at once. Every id is checked before anything is "
            "deleted, so a batch containing one user you may not remove removes none."
        ),
    )
    @action(detail=False, methods=["post"])
    def bulk_delete(self, request):
        """Overridden, not inherited.

        `BaseViewSet.bulk_delete` calls `soft_delete` on each row directly rather than
        going through `perform_destroy`, so inheriting it would have left a second
        removal path with neither the role check nor the seat release on it — the two
        things this class adds.
        """
        from django.db import transaction
        from rest_framework import status as http_status

        ids = request.data.get("ids", [])
        if not isinstance(ids, list) or not ids:
            return self.error(
                message="ids must be a non-empty list",
                status_code=http_status.HTTP_400_BAD_REQUEST,
            )

        queryset = self.get_queryset().filter(id__in=ids, is_deleted=False)
        if queryset.count() != len(set(ids)):
            return self.error(
                message="Some ids are invalid",
                status_code=http_status.HTTP_400_BAD_REQUEST,
            )

        targets = list(queryset)
        # All-or-nothing: checked up front so a batch is never half-applied, and so the
        # response cannot be used to discover which single id was the protected one.
        for target in targets:
            membership.assert_can_act_on(request.user, target)

        with transaction.atomic():
            for target in targets:
                membership.release_seat(target, deleted_by=request.user)
                target.soft_delete(user=request.user)

        return self.success(message="Bulk deleted successfully")

    @action(detail=False, methods=["get"], url_path="me")
    def me(self, request):
        serializer = self.get_serializer(request.user)
        return self.success(data=serializer.data)

    @extend_schema(
        summary="Set a user's role",
        description=(
            "Promote or demote somebody between `ADMIN` and `MEMBER`. `OWNER` cannot "
            "be set here — ownership moves through `transfer-ownership/`, which leaves "
            "exactly one owner rather than creating a second."
        ),
        request=SetRoleSerializer,
        responses={
            200: UserSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Invalid role."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied."),
        },
    )
    @action(detail=True, methods=["post"], url_path="set-role")
    def set_role(self, request, *args, **kwargs):
        from tenant.models.TenantUserMapping import TenantUserMapping

        target = self.get_object()
        # Both checks, and the first is not redundant: `assert_can_act_on` permits
        # acting on yourself, so on its own it would let a member promote themselves
        # to admin. Managing roles requires being a manager first.
        membership.assert_can_manage_members(request.user)
        membership.assert_can_act_on(request.user, target)

        serializer = SetRoleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        role = serializer.validated_data["role"]

        mapping = membership.mapping_for(target)
        if mapping is None:
            return self.error(
                message="That user does not belong to an organisation.",
                status_code=status.HTTP_409_CONFLICT,
            )

        mapping.role = role
        mapping.updated_by = request.user
        mapping.save(update_fields=["role", "updated_by", "updated_at"])

        return self.success(
            data=self.get_serializer(target).data, message="Role updated"
        )

    @extend_schema(
        summary="Transfer ownership",
        description=(
            "Hand `OWNER` to another member of the organisation. Only the current "
            "owner may call this, and they become an `ADMIN` in the same step — a "
            "tenant always has exactly one owner."
        ),
        request=None,
        responses={
            200: UserSerializer,
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Only the owner may transfer ownership."),
        },
    )
    @action(detail=True, methods=["post"], url_path="transfer-ownership")
    def transfer_ownership(self, request, *args, **kwargs):
        # No assert_can_act_on here: that refuses any action *targeting* the owner,
        # which is right everywhere else and wrong here. The service does the check
        # this action actually needs — that the caller IS the owner.
        target = self.get_object()
        membership.transfer_ownership(request.user, target)

        return self.success(
            data=self.get_serializer(target).data, message="Ownership transferred"
        )

    @extend_schema(
        summary="Change Password",
        description="Allows an authenticated user to change their password.",
        request=ChangePasswordSerializer,
        responses={
            200: OpenApiResponse(response=GeneralResponseSerializer, description="Password changed successfully."),
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation errors."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request.")
        }
    )
    @action(detail=False, methods=["post"], url_path="change-password")
    def change_password(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        user = request.user
        new_password = serializer.validated_data["new_password"]
        user.set_password(new_password)
        user.save()

        return self.success(message="Password changed successfully.")
