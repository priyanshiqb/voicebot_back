from onboard.models.timezone import Timezone
from onboard.serializers.timezone import (
    TimezoneSerializer,
    TimezoneListSerializer,
    TimezoneDropdownSerializer,
)
from utils.views.base_model_viewset import BaseViewSet
from rest_framework.permissions import AllowAny
from utils.permissions.is_superuser import IsSuperUser
from drf_spectacular.utils import (
    extend_schema,
    extend_schema_view,
    OpenApiParameter,
    OpenApiTypes,
    OpenApiResponse,
)
from onboard.serializers.onboarding import GeneralResponseSerializer


@extend_schema(tags=["Onboarding - Timezones"])
@extend_schema_view(
    list=extend_schema(
        summary="List Timezones",
        description="Get paginated list of timezones with optional search, ordering, and filter attributes.",
        parameters=[
            OpenApiParameter(
                name="is_active",
                type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
            ),
            OpenApiParameter(
                name="offset",
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
            200: TimezoneListSerializer(many=True),
        }
    ),
    create=extend_schema(
        summary="Create Timezone",
        description="Create a new timezone option.",
        responses={
            201: TimezoneSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied (superuser required).")
        }
    ),
    retrieve=extend_schema(
        summary="Get Timezone",
        description="Get specific timezone details by ID.",
        responses={
            200: TimezoneSerializer,
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Timezone not found.")
        }
    ),
    update=extend_schema(
        summary="Update Timezone",
        description="Completely update an existing timezone by ID.",
        responses={
            200: TimezoneSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied (superuser required)."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Timezone not found.")
        }
    ),
    partial_update=extend_schema(
        summary="Partial Update Timezone",
        description="Partially update an existing timezone by ID.",
        responses={
            200: TimezoneSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied (superuser required)."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Timezone not found.")
        }
    ),
    destroy=extend_schema(
        summary="Delete Timezone",
        description="Soft-delete an existing timezone by ID.",
        responses={
            200: OpenApiResponse(response=GeneralResponseSerializer, description="Timezone deleted successfully."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied (superuser required)."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Timezone not found.")
        }
    ),
    dropdown=extend_schema(
        summary="Get Timezone Dropdown Options",
        description="Get lightweight label/value selection pairs representing all active timezones.",
        responses={
            200: TimezoneDropdownSerializer(many=True)
        }
    )
)
class TimezoneViewSet(BaseViewSet):
    """
    Timezone ViewSet.
    Provides robust, production-grade endpoints for managing system timezones.
    """
    queryset = Timezone.objects.all()

    serializer_class = TimezoneSerializer
    list_serializer_class = TimezoneListSerializer
    dropdown_serializer_class = TimezoneDropdownSerializer

    search_fields = [
        "timezone",
        "offset",
        "region",
    ]

    ordering_fields = [
        "timezone",
        "offset",
        "created_at",
    ]

    ordering = ["timezone"]

    filterset_fields = {
        "timezone": ["exact"],
        "offset": ["exact"],
        "region": ["exact", "icontains"],
        "is_active": ["exact"],
        "created_at": ["date", "gte", "lte"],
    }

    def get_permissions(self):
        """
        Allows unauthenticated access (AllowAny) for list, retrieve, and dropdown methods.
        Only superusers (IsSuperUser) can perform timezone mutations (create, update, delete).
        """
        if self.action in ["list", "retrieve", "dropdown"]:
            return [AllowAny()]
        return [IsSuperUser()]
