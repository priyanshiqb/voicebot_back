from onboard.models.plan import Plan
from onboard.serializers.plan import (
    PlanSerializer,
    PlanListSerializer,
    PlanDropdownSerializer,
)
from utils.views.base_model_viewset import BaseViewSet
from rest_framework.permissions import AllowAny
from drf_spectacular.utils import (
    extend_schema,
    extend_schema_view,
    OpenApiParameter,
    OpenApiTypes,
    OpenApiResponse,
)
from onboard.serializers.onboarding import GeneralResponseSerializer


@extend_schema(tags=["Onboarding - Plans"])
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
        responses={
            200: PlanListSerializer(many=True),
        }
    ),
    create=extend_schema(
        summary="Create Plan",
        description="Create a new subscription plan.",
        responses={
            201: PlanSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied (superuser required).")
        }
    ),
    retrieve=extend_schema(
        summary="Get Plan",
        description="Get specific plan details by ID.",
        responses={
            200: PlanSerializer,
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Plan not found.")
        }
    ),
    update=extend_schema(
        summary="Update Plan",
        description="Completely update an existing plan by ID.",
        responses={
            200: PlanSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied (superuser required)."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Plan not found.")
        }
    ),
    partial_update=extend_schema(
        summary="Partial Update Plan",
        description="Partially update an existing plan by ID.",
        responses={
            200: PlanSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied (superuser required)."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Plan not found.")
        }
    ),
    destroy=extend_schema(
        summary="Delete Plan",
        description="Soft-delete an existing plan by ID.",
        responses={
            200: OpenApiResponse(response=GeneralResponseSerializer, description="Plan deleted successfully."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied (superuser required)."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Plan not found.")
        }
    ),
    dropdown=extend_schema(
        summary="Get Dropdown Options",
        description="Get lightweight label/value selection pairs representing all active plans.",
        responses={
            200: PlanDropdownSerializer(many=True)
        }
    )
)
class PlanViewSet(BaseViewSet):
    """
    Plan ViewSet.
    Provides robust, production-grade endpoints for managing billing and subscription plans.
    """
    queryset = Plan.objects.all()

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action in ["list", "dropdown"]:
            return queryset.filter(is_active=True)
        return queryset

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

    # **Read-only. There is no write path here for anybody, superuser included.**
    #
    # This is the public pricing-page API -- `AllowAny` on read -- and it used to
    # accept writes from a superuser. That was a second door into the catalogue with
    # no audit trail and no completeness gate: a plan created through it was
    # instantly `is_active`, with zero entitlements and zero rates. A missing
    # allowance is not a limit of zero but no limit at all, and a missing rate cannot
    # price a call, so that plan was unlimited and unbillable at the same time --
    # silently, because neither failure raises anything.
    #
    # Blocked at the verb rather than the permission. A permission check is one
    # `get_permissions` override away from being loosened by someone who does not
    # know why it is there; `http_method_names` removes the routes, and
    # `bulk_delete` (a POST action inherited from BaseViewSet) goes with them.
    #
    # Catalogue writes live at `/api/v1/ops/plans/`, where they are gated on
    # `plan.change`, refused unless the plan is complete, and recorded.
    http_method_names = ["get", "head", "options"]

    def get_permissions(self):
        """Public read. Every action left on this viewset is one."""
        return [AllowAny()]
