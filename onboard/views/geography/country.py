from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.db.models import Q, Case, When, Value, IntegerField
from onboard.models import Country
from onboard.serializers import CountryDropdownSerializer
from .geography_pagination import GeographyDropdownPagination
from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.types import OpenApiTypes
from onboard.authentication import CombinedOnboardingAuthentication
from .mixins import GeographyTimeoutMixin

class CountryDropdownAPIView(GeographyTimeoutMixin, APIView):
    authentication_classes = [CombinedOnboardingAuthentication]
    permission_classes = (IsAuthenticated,)
    
    @extend_schema(
        tags=["Geography"],
        parameters=[
            OpenApiParameter(name="code", description="Filter by country code", required=False, type=OpenApiTypes.STR),
            OpenApiParameter(name="name", description="Filter exactly by country name", required=False, type=OpenApiTypes.STR),
            OpenApiParameter(name="search", description="Partial search on name or dial_code (e.g. +91 or 91)", required=False, type=OpenApiTypes.STR),
            OpenApiParameter(name="page", description="Page number", required=False, type=OpenApiTypes.INT),
            OpenApiParameter(name="page_size", description="Number of results per page", required=False, type=OpenApiTypes.INT),
        ],
        responses={200: CountryDropdownSerializer(many=True)}
    )
    def get(self, request, *args, **kwargs):
        # Order India (code='IN') at the top, then by default ordering (name)
        queryset = Country.objects.annotate(
            is_india=Case(
                When(code='IN', then=Value(0)),
                default=Value(1),
                output_field=IntegerField()
            )
        ).order_by('is_india', 'name')
        
        code = request.query_params.get('code')
        if code:
            queryset = queryset.filter(code=code)
            
        name = request.query_params.get('name')
        if name:
            queryset = queryset.filter(name=name)
            
        search = request.query_params.get('search')
        if search:
            # If the client sent a raw '+' it might decode as a space, so we check for both
            search_normalized = search.replace(' ', '+') if search.startswith(' ') else search
            queryset = queryset.filter(
                Q(name__icontains=search) | 
                Q(dial_code__icontains=search) |
                Q(dial_code__icontains=search_normalized)
            )
            
        paginator = GeographyDropdownPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        if page is not None:
            serializer = CountryDropdownSerializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)
            
        serializer = CountryDropdownSerializer(queryset, many=True)
        return Response({
            "status": True,
            "message": "success",
            "data": serializer.data,
            "errors": None
        })
