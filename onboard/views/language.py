from onboard.models.language import Language
from onboard.serializers.language import (
    LanguageSerializer,
    LanguageListSerializer,
    LanguageDropdownSerializer,
)
from utils.views.base_model_viewset import BaseViewSet
from rest_framework.permissions import IsAuthenticated
from utils.permissions.is_superuser import IsSuperUser
from drf_spectacular.utils import (
    extend_schema,
    extend_schema_view,
    OpenApiParameter,
    OpenApiTypes,
    OpenApiResponse,
)
from onboard.serializers.onboarding import GeneralResponseSerializer


@extend_schema(tags=["Onboarding - Languages"])
@extend_schema_view(
    list=extend_schema(
        summary="List Languages",
        description="Get paginated list of languages with optional search, ordering, and filter attributes.",
        parameters=[
            OpenApiParameter(
                name="is_active",
                type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
            ),
            OpenApiParameter(
                name="code",
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
            200: LanguageListSerializer(many=True),
        }
    ),
    create=extend_schema(
        summary="Create Language",
        description="Create a new language option.",
        responses={
            201: LanguageSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied (superuser required).")
        }
    ),
    retrieve=extend_schema(
        summary="Get Language",
        description="Get specific language details by ID.",
        responses={
            200: LanguageSerializer,
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Language not found.")
        }
    ),
    update=extend_schema(
        summary="Update Language",
        description="Completely update an existing language by ID.",
        responses={
            200: LanguageSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied (superuser required)."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Language not found.")
        }
    ),
    partial_update=extend_schema(
        summary="Partial Update Language",
        description="Partially update an existing language by ID.",
        responses={
            200: LanguageSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied (superuser required)."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Language not found.")
        }
    ),
    destroy=extend_schema(
        summary="Delete Language",
        description="Soft-delete an existing language by ID.",
        responses={
            200: OpenApiResponse(response=GeneralResponseSerializer, description="Language deleted successfully."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied (superuser required)."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Language not found.")
        }
    ),
    dropdown=extend_schema(
        summary="Get Language Dropdown Options",
        description="Get lightweight label/value selection pairs representing all active languages.",
        responses={
            200: LanguageDropdownSerializer(many=True)
        }
    )
)
class LanguageViewSet(BaseViewSet):
    """
    Language ViewSet.
    Provides robust, production-grade endpoints for managing system languages.
    """
    queryset = Language.objects.all()

    serializer_class = LanguageSerializer
    list_serializer_class = LanguageListSerializer
    dropdown_serializer_class = LanguageDropdownSerializer

    search_fields = [
        "name",
        "code",
    ]

    ordering_fields = [
        "name",
        "code",
        "created_at",
    ]

    ordering = ["name"]

    filterset_fields = {
        "name": ["exact", "icontains"],
        "code": ["exact"],
        "is_active": ["exact"],
        "created_at": ["date", "gte", "lte"],
    }

    def get_permissions(self):
        """
        Allows authenticated access (IsAuthenticated) for list, retrieve, and dropdown methods.
        Only superusers (IsSuperUser) can perform language mutations (create, update, delete).
        """
        if self.action in ["list", "retrieve", "dropdown"]:
            return [IsAuthenticated()]
        return [IsSuperUser()]
