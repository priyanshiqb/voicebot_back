from user.serializers.custom_user import (
    UserSerializer,
    UserListSerializer,
    UserDropdownSerializer,
)
from user.models.CustomUser import CustomUser
from django.db import models
from utils.views.base_model_viewset import BaseViewSet
from drf_spectacular.utils import (
    extend_schema,
    extend_schema_view,
    OpenApiParameter,
    OpenApiTypes,
)
from rest_framework.decorators import action


@extend_schema_view(
    list=extend_schema(
        summary="List Users",
        description="Get paginated list of users",
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
    ),
    create=extend_schema(
        summary="Create User",
        description="Create a new user"
    ),
    retrieve=extend_schema(
        summary="Get User"
    ),
    update=extend_schema(
        summary="Update User"
    ),
    partial_update=extend_schema(
        summary="Partial Update User"
    ),
    destroy=extend_schema(
        summary="Delete User"
    ),
    me=extend_schema(
        summary="Get Current User",
        description="Get details of the currently authenticated user"
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
        return super().get_queryset()

    @action(detail=False, methods=["get"], url_path="me")
    def me(self, request):
        serializer = self.get_serializer(request.user)
        return self.success(data=serializer.data)