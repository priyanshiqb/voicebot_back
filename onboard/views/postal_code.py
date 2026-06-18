from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed, APIException
from user.authentication import CachedJWTAuthentication
from onboard.authentication import TempUserAuthentication
from onboard.views.onboarding.base import IsTempUserAuthenticated
from django.db import transaction, connection
from django.db.utils import OperationalError
from django.db.models import F, FloatField, Q, Value, Func
from django.db.models.functions import ACos, Cos, Radians, Sin, Least, Greatest, Cast
from drf_spectacular.utils import (
    extend_schema,
    extend_schema_view,
    OpenApiParameter,
    OpenApiTypes,
    OpenApiResponse,
)

from onboard.models.postal_code import PostalCode
from onboard.serializers.postal_code import (
    PostalCodeSerializer,
    PostalCodeListSerializer,
    PostalCodeDropdownSerializer,
)
from onboard.serializers.onboarding import GeneralResponseSerializer
from utils.views.base_model_viewset import BaseViewSet
from utils.permissions.is_superuser import IsSuperUser


class QueryTimeoutException(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "Database query timed out. Please refine your search filters to narrow down results."
    default_code = "query_timeout"


class CombinedOnboardingAuthentication(BaseAuthentication):
    """
    Combined authentication class that tries CachedJWTAuthentication first.
    If that fails, it falls back to TempUserAuthentication.
    Only if both fail does it raise an AuthenticationFailed exception.
    """
    def authenticate(self, request):
        jwt_auth = CachedJWTAuthentication()
        temp_auth = TempUserAuthentication()

        # Try CachedJWTAuthentication
        try:
            jwt_res = jwt_auth.authenticate(request)
            if jwt_res is not None:
                return jwt_res
        except AuthenticationFailed:
            # Ignore JWT failures and try TempUserAuthentication
            pass

        # Try TempUserAuthentication
        try:
            temp_res = temp_auth.authenticate(request)
            if temp_res is not None:
                return temp_res
        except AuthenticationFailed:
            # If temp user auth failed with an explicit error, bubble it up
            raise

        return None

    def authenticate_header(self, request):
        return "Bearer"


try:
    from drf_spectacular.extensions import OpenApiAuthenticationExtension

    class CombinedOnboardingAuthenticationScheme(OpenApiAuthenticationExtension):
        target_class = "onboard.views.postal_code.CombinedOnboardingAuthentication"
        name = "CombinedOnboardingAuth"

        def get_security_definition(self, auto_schema):
            # This is a combined scheme that delegates to JWT and TempUserTokenAuth.
            # We return an empty dict since it references the target security definitions.
            return {}

        def get_security_requirement(self, auto_schema):
            return [
                {"jwt": []},
                {"TempUserTokenAuth": []}
            ]
except ImportError:
    pass



@extend_schema(tags=["Onboarding - Postal Codes"])
@extend_schema_view(
    list=extend_schema(
        summary="List Postal Codes",
        description="Get paginated list of global postal codes with search, ordering, and filtering capabilities.",
        parameters=[
            OpenApiParameter(name="is_active", type=OpenApiTypes.BOOL, location=OpenApiParameter.QUERY),
            OpenApiParameter(name="country_code", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY),
            OpenApiParameter(name="postal_code", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY),
            OpenApiParameter(name="place_name", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY),
            OpenApiParameter(name="search", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY),
        ],
        responses={200: PostalCodeListSerializer(many=True)}
    ),
    create=extend_schema(
        summary="Create Postal Code",
        description="Create a new postal code reference record.",
        responses={
            201: PostalCodeSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied (superuser required).")
        }
    ),
    retrieve=extend_schema(
        summary="Get Postal Code",
        description="Get specific postal code details by ID.",
        responses={
            200: PostalCodeSerializer,
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Postal code not found.")
        }
    ),
    update=extend_schema(
        summary="Update Postal Code",
        description="Completely update an existing postal code record.",
        responses={
            200: PostalCodeSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied (superuser required)."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Postal code not found.")
        }
    ),
    partial_update=extend_schema(
        summary="Partial Update Postal Code",
        description="Partially update an existing postal code record.",
        responses={
            200: PostalCodeSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied (superuser required)."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Postal code not found.")
        }
    ),
    destroy=extend_schema(
        summary="Delete Postal Code",
        description="Soft-delete an existing postal code record by ID.",
        responses={
            200: OpenApiResponse(response=GeneralResponseSerializer, description="Postal code deleted successfully."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied (superuser required)."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Postal code not found.")
        }
    ),
    dropdown=extend_schema(
        summary="Get Postal Code Dropdown Options",
        description="Get lightweight label/value selection pairs representing all active postal codes.",
        responses={200: PostalCodeDropdownSerializer(many=True)}
    )
)
class PostalCodeViewSet(BaseViewSet):
    """
    PostalCode ViewSet.
    Provides robust, production-grade endpoints for managing global postal code data.
    """
    queryset = PostalCode.objects.all()
    authentication_classes = [CombinedOnboardingAuthentication]

    serializer_class = PostalCodeSerializer
    list_serializer_class = PostalCodeListSerializer
    dropdown_serializer_class = PostalCodeDropdownSerializer

    search_fields = [
        "postal_code",
        "place_name",
        "country_code",
        "admin_name1",
    ]

    ordering_fields = [
        "country_code",
        "postal_code",
        "place_name",
    ]

    ordering = ["country_code", "postal_code", "place_name"]

    filterset_fields = {
        "country_code": ["exact", "icontains"],
        "postal_code": ["exact", "icontains"],
        "place_name": ["exact", "icontains"],
        "is_active": ["exact"],
    }

    def filter_queryset(self, queryset):
        """
        Custom filtering/searching logic for postal codes to ignore spaces.
        """
        query_params = self.request.query_params.copy()
        postal_code_exact = query_params.get("postal_code")
        postal_code_icontains = query_params.get("postal_code__icontains")
        search_term = query_params.get("search")

        if postal_code_exact or postal_code_icontains or search_term:
            # Annotate with stripped postal code ONLY when active search/filtering is requested
            queryset = queryset.annotate(
                postal_code_stripped=Func(
                    F('postal_code'),
                    Value(' '),
                    Value(''),
                    function='REPLACE'
                )
            )

        # 1. Handle exact postal code filter (ignoring spaces)
        if postal_code_exact:
            stripped = postal_code_exact.replace(" ", "")
            queryset = queryset.filter(postal_code_stripped=stripped)
            query_params.pop("postal_code", None)

        # 2. Handle icontains postal code filter (ignoring spaces)
        if postal_code_icontains:
            stripped = postal_code_icontains.replace(" ", "")
            queryset = queryset.filter(postal_code_stripped__icontains=stripped)
            query_params.pop("postal_code__icontains", None)

        # 3. Handle search parameter (ignoring spaces on postal_code field)
        if search_term:
            stripped_search = search_term.replace(" ", "")
            queryset = queryset.filter(
                Q(postal_code_stripped__icontains=stripped_search) |
                Q(place_name__icontains=search_term) |
                Q(country_code__icontains=search_term) |
                Q(admin_name1__icontains=search_term)
            )
            query_params.pop("search", None)

        # Temporarily mock the query params so DjangoFilterBackend and SearchFilter
        # do not apply standard filters on original fields.
        original_get = self.request._request.GET
        self.request._request.GET = query_params

        original_query_params = getattr(self.request, "_query_params", None)
        self.request._query_params = query_params

        try:
            filtered_queryset = super().filter_queryset(queryset)
        finally:
            self.request._request.GET = original_get
            if original_query_params is not None:
                self.request._query_params = original_query_params
            else:
                try:
                    delattr(self.request, "_query_params")
                except AttributeError:
                    pass

        return filtered_queryset

    def get_permissions(self):
        """
        Allows authenticated access (IsAuthenticated or IsTempUserAuthenticated) for list, retrieve, dropdown, and nearest.
        Only superusers (IsSuperUser) can perform postal code mutations (create, update, delete).
        """
        if self.action in ["list", "retrieve", "dropdown", "nearest"]:
            return [(IsAuthenticated | IsTempUserAuthenticated)()]
        return [IsSuperUser()]

    def _execute_with_timeout(self, timeout_ms, query_func):
        """
        Helper method to execute a query function within a transaction block
        and a PostgreSQL statement timeout. Falls back to normal execution
        on other database backends (e.g. SQLite for unit tests).
        """
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    if connection.vendor == 'postgresql':
                        cursor.execute(f"SET LOCAL statement_timeout = {timeout_ms};")
                return query_func()
        except OperationalError as e:
            if 'canceling statement due to statement timeout' in str(e):
                raise QueryTimeoutException()
            raise

    def list(self, request, *args, **kwargs):
        """
        Get paginated list of global postal codes with query timeout protection
        and a hard-capped page size.
        """
        queryset = self.filter_queryset(self.get_queryset())

        def run_query():
            page = self.paginate_queryset(queryset)
            if page is not None:
                serializer = self.get_serializer(page, many=True)
                return self.get_paginated_response(serializer.data)

            # Hard limit of 100 records if pagination is bypassed/disabled
            limited_queryset = queryset[:100]
            serializer = self.get_serializer(limited_queryset, many=True)
            return self.success(data=serializer.data, message="Listed successfully")

        return self._execute_with_timeout(2000, run_query)

    @action(detail=False, methods=["get"])
    def dropdown(self, request):
        """
        Get lightweight dropdown options with query timeout protection and
        a hard limit of 100 records.
        """
        queryset = self.filter_queryset(self.get_queryset())

        def run_query():
            limit_param = request.query_params.get("limit")
            limit = 100
            if limit_param and limit_param.isdigit():
                limit = min(int(limit_param), 100)

            limited_queryset = queryset[:limit]
            serializer = self.get_serializer(limited_queryset, many=True)
            return self.success(data=serializer.data, message="Dropdown options retrieved successfully")

        return self._execute_with_timeout(2000, run_query)

    @extend_schema(
        summary="Get Nearest Postal Code",
        description="Find the closest active postal code to the given latitude and longitude coordinates.",
        parameters=[
            OpenApiParameter(
                name="latitude",
                type=OpenApiTypes.DECIMAL,
                location=OpenApiParameter.QUERY,
                required=True,
                description="Latitude of target coordinate (e.g. 25.293300)"
            ),
            OpenApiParameter(
                name="longitude",
                type=OpenApiTypes.DECIMAL,
                location=OpenApiParameter.QUERY,
                required=True,
                description="Longitude of target coordinate (e.g. 55.339900)"
            ),
        ],
        responses={
            200: PostalCodeSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="No postal codes found.")
        }
    )
    @action(detail=False, methods=["get"], url_path="nearest")
    def nearest(self, request):
        latitude_param = request.query_params.get("latitude")
        longitude_param = request.query_params.get("longitude")

        if not latitude_param or not longitude_param:
            return Response(
                {"detail": "Both latitude and longitude query parameters are required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            latitude = float(latitude_param)
            longitude = float(longitude_param)
        except ValueError:
            return Response(
                {"detail": "Latitude and longitude must be valid floating point numbers."},
                status=status.HTTP_400_BAD_REQUEST
            )

        if latitude < -90 or latitude > 90:
            return Response(
                {"detail": "Latitude must be between -90 and 90 degrees."},
                status=status.HTTP_400_BAD_REQUEST
            )

        if longitude < -180 or longitude > 180:
            return Response(
                {"detail": "Longitude must be between -180 and 180 degrees."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Great-circle distance calculations using Spherical Law of Cosines.
        # Clamped with Least/Greatest to avoid domain errors in ACos from float precision.
        rad_lat = Radians(latitude)
        rad_lon = Radians(longitude)

        rad_db_lat = Radians(Cast(F("latitude"), output_field=FloatField()))
        rad_db_lon = Radians(Cast(F("longitude"), output_field=FloatField()))

        cos_distance = (
            Sin(rad_lat) * Sin(rad_db_lat) +
            Cos(rad_lat) * Cos(rad_db_lat) * Cos(rad_db_lon - rad_lon)
        )
        clamped_cos = Least(Greatest(cos_distance, -1.0), 1.0)
        distance_expr = 6371 * ACos(clamped_cos)

        # Filter for records that have geographical coordinates and are active
        queryset = self.get_queryset().filter(
            latitude__isnull=False,
            longitude__isnull=False,
            is_active=True
        )

        def run_query():
            return queryset.annotate(distance=distance_expr).order_by("distance").first()

        closest_postal_code = self._execute_with_timeout(3000, run_query)

        if not closest_postal_code:
            return Response(
                {"detail": "No active postal codes with coordinates found in the system."},
                status=status.HTTP_404_NOT_FOUND
            )

        serializer = self.get_serializer(closest_postal_code)
        return Response(serializer.data, status=status.HTTP_200_OK)
