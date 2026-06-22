from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from onboard.models import Admin1
from onboard.serializers import Admin1DropdownSerializer
from .geography_pagination import GeographyDropdownPagination
from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.types import OpenApiTypes
from onboard.authentication import CombinedOnboardingAuthentication

class Admin1DropdownAPIView(APIView):
    authentication_classes = [CombinedOnboardingAuthentication]
    permission_classes = (IsAuthenticated,)
    
    @extend_schema(
        tags=["Geography"],
        parameters=[
            OpenApiParameter(name="country_id", description="Filter by parent country UUID", required=True, type=OpenApiTypes.UUID),
            OpenApiParameter(name="name", description="Filter exactly by state name", required=False, type=OpenApiTypes.STR),
            OpenApiParameter(name="search", description="Partial search on name", required=False, type=OpenApiTypes.STR),
            OpenApiParameter(name="page", description="Page number", required=False, type=OpenApiTypes.INT),
            OpenApiParameter(name="page_size", description="Number of results per page", required=False, type=OpenApiTypes.INT),
        ],
        responses={200: Admin1DropdownSerializer(many=True), 400: None}
    )
    def get(self, request, *args, **kwargs):
        country_id = request.query_params.get('country_id')
        if not country_id:
            return Response({
                "status": False,
                "message": "country_id parameter is required",
                "errors": {"code": "VALIDATION_ERROR"}
            }, status=status.HTTP_400_BAD_REQUEST)
            
        queryset = Admin1.objects.filter(country_id=country_id)
        
        # specific NO_FURTHER_DATA logic
        if not queryset.exists():
            return Response({
                "status": False,
                "message": "No states found for this country",
                "errors": {"code": "NO_FURTHER_DATA"}
            }, status=status.HTTP_400_BAD_REQUEST)
        
        name = request.query_params.get('name')
        if name:
            queryset = queryset.filter(name=name)
            
        search = request.query_params.get('search')
        if search:
            queryset = queryset.filter(name__icontains=search)
            
        paginator = GeographyDropdownPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        if page is not None:
            serializer = Admin1DropdownSerializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)
            
        serializer = Admin1DropdownSerializer(queryset, many=True)
        return Response({
            "status": True,
            "message": "success",
            "data": serializer.data,
            "errors": None
        })
