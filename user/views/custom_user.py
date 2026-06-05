from user.serializers.custom_user import (
    UserSerializer,
    UserListSerializer,
    UserDropdownSerializer,
    ChangePasswordSerializer,
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
class CustomUserViewSet(BaseViewSet):

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

        if user.is_superuser:
            return queryset

        try:
            active_tenant = user.tenant_mapping.tenant
            return queryset.filter(tenant_mapping__tenant=active_tenant)
        except Exception:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Your user account is not associated with any active tenant.")

    def perform_create(self, serializer):
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
                        created_by=user,
                        updated_by=user
                    )
                except Exception:
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("Your user account is not associated with any active tenant.")

    @action(detail=False, methods=["get"], url_path="me")
    def me(self, request):
        serializer = self.get_serializer(request.user)
        return self.success(data=serializer.data)

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