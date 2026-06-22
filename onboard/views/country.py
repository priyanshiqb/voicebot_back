from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.db.models import Q
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
            OpenApiParameter(name="search", description="Partial search on name", required=False, type=OpenApiTypes.STR),
            OpenApiParameter(name="page", description="Page number", required=False, type=OpenApiTypes.INT),
            OpenApiParameter(name="page_size", description="Number of results per page", required=False, type=OpenApiTypes.INT),
        ],
        responses={200: CountryDropdownSerializer(many=True)}
    )
    def get(self, request, *args, **kwargs):
        queryset = Country.objects.all()
        
        code = request.query_params.get('code')
        if code:
            queryset = queryset.filter(code=code)
            
        name = request.query_params.get('name')
        if name:
            queryset = queryset.filter(name=name)
            
        search = request.query_params.get('search')
        if search:
            queryset = queryset.filter(name__icontains=search)
            
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
