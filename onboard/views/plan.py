from onboard.models.plan import Plan
from onboard.serializers.plan import (
    PlanSerializer,
    PlanListSerializer,
    PlanDropdownSerializer,
)
from utils.views.base_model_viewset import BaseViewSet
from rest_framework.permissions import AllowAny
from utils.permissions.is_superuser import IsSuperUser
from drf_spectacular.utils import (
    extend_schema,
    extend_schema_view,
    OpenApiParameter,
    OpenApiTypes,
)


@extend_schema_view(
    list=extend_schema(
        summary="List Plans",
        description="Get paginated list of plans with optional search, ordering, and filter attributes.",
        parameters=[
            OpenApiParameter(
                name="is_active",
                type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
            ),
            OpenApiParameter(
                name="plan_type",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
            ),
            OpenApiParameter(
                name="currency",
                type=OpenApiTypes.STR,
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
        summary="Create Plan",
        description="Create a new subscription plan."
    ),
    retrieve=extend_schema(
        summary="Get Plan",
        description="Get specific plan details by ID."
    ),
    update=extend_schema(
        summary="Update Plan",
        description="Completely update an existing plan by ID."
    ),
    partial_update=extend_schema(
        summary="Partial Update Plan",
        description="Partially update an existing plan by ID."
    ),
    destroy=extend_schema(
        summary="Delete Plan",
        description="Soft-delete an existing plan by ID."
    ),
    dropdown=extend_schema(
        summary="Get Dropdown Options",
        description="Get lightweight label/value selection pairs representing all active plans."
    )
)
class PlanViewSet(BaseViewSet):
    """
    Plan ViewSet.
    Provides robust, production-grade endpoints for managing billing and subscription plans.
    """
    queryset = Plan.objects.all()

    serializer_class = PlanSerializer
    list_serializer_class = PlanListSerializer
    dropdown_serializer_class = PlanDropdownSerializer

    search_fields = [
        "code",
        "name",
        "description",
    ]

    ordering_fields = [
        "name",
        "base_price",
        "sort_order",
        "created_at",
    ]

    filterset_fields = {
        "code": ["exact"],
        "is_active": ["exact"],
        "plan_type": ["exact"],
        "currency": ["exact"],
        "billing_cycle": ["exact"],
        "base_price": ["gte", "lte"],
        "tax_percentage": ["gte", "lte"],
        "discount_percentage": ["gte", "lte"],
        "created_at": ["date", "gte", "lte"],
    }

    def get_permissions(self):
        """
        Instantiates and returns the list of permissions that this view requires.
        Allows unauthenticated access (AllowAny) for list, retrieve, and dropdown methods.
        Only superusers (IsSuperUser) can perform plan mutations (create, update, delete).
        """
        if self.action in ["list", "retrieve", "dropdown"]:
            return [AllowAny()]
        return [IsSuperUser()]
