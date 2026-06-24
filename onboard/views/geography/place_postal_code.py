from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.db.models import Func, F, Value, Case, When, IntegerField
from onboard.models import Place
from onboard.serializers.geography.place import PlacePostalCodeDropdownSerializer
from .geography_pagination import GeographyDropdownPagination
from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.types import OpenApiTypes
from onboard.authentication import CombinedOnboardingAuthentication
from .mixins import GeographyTimeoutMixin

class PlacePostalCodeDropdownAPIView(GeographyTimeoutMixin, APIView):
    authentication_classes = [CombinedOnboardingAuthentication]
    permission_classes = (IsAuthenticated,)
    
    @extend_schema(
        tags=["Geography"],
        parameters=[
            OpenApiParameter(name="search", description="Search postal code ignoring spaces", required=False, type=OpenApiTypes.STR),
            OpenApiParameter(name="postal_code", description="Filter exactly by postal code", required=False, type=OpenApiTypes.STR),
            OpenApiParameter(name="country_id", description="Filter by country ID", required=False, type=OpenApiTypes.UUID),
            OpenApiParameter(name="admin1_id", description="Filter by admin1 ID", required=False, type=OpenApiTypes.UUID),
            OpenApiParameter(name="admin2_id", description="Filter by admin2 ID", required=False, type=OpenApiTypes.UUID),
            OpenApiParameter(name="admin3_id", description="Filter by admin3 ID", required=False, type=OpenApiTypes.UUID),
            OpenApiParameter(name="page", description="Page number", required=False, type=OpenApiTypes.INT),
            OpenApiParameter(name="page_size", description="Number of results per page", required=False, type=OpenApiTypes.INT),
        ],
        responses={200: PlacePostalCodeDropdownSerializer(many=True)}
    )
    def get(self, request, *args, **kwargs):
        queryset = Place.objects.all()
        
        country_id = request.query_params.get('country_id')
        if country_id:
            queryset = queryset.filter(country_id=country_id)
            
        admin1_id = request.query_params.get('admin1_id')
        if admin1_id:
            queryset = queryset.filter(admin1_id=admin1_id)
            
        admin2_id = request.query_params.get('admin2_id')
        if admin2_id:
            queryset = queryset.filter(admin2_id=admin2_id)
            
        admin3_id = request.query_params.get('admin3_id')
        if admin3_id:
            queryset = queryset.filter(admin3_id=admin3_id)
            
        postal_code = request.query_params.get('postal_code')
        if postal_code:
            queryset = queryset.filter(postal_code=postal_code)
            
        search = request.query_params.get('search')
        if search:
            stripped_search = search.replace(" ", "")
            queryset = queryset.annotate(
                stripped_postal=Func(F('postal_code'), Value(' '), Value(''), function='REPLACE')
            ).filter(stripped_postal__istartswith=stripped_search).annotate(
                priority=Case(
                    When(stripped_postal=stripped_search, then=Value(1)),
                    default=Value(2),
                    output_field=IntegerField()
                )
            ).order_by('priority', 'postal_code', 'name')
        else:
            queryset = queryset.order_by('postal_code', 'name')
            
        paginator = GeographyDropdownPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        if page is not None:
            serializer = PlacePostalCodeDropdownSerializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)
            
        serializer = PlacePostalCodeDropdownSerializer(queryset, many=True)
        return Response({
            "status": True,
            "message": "success",
            "data": serializer.data,
            "errors": None
        })
