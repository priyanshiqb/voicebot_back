from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from onboard.models import Admin2
from onboard.serializers import Admin2DropdownSerializer
from .geography_pagination import GeographyDropdownPagination
from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.types import OpenApiTypes
from onboard.authentication import CombinedOnboardingAuthentication

class Admin2DropdownAPIView(APIView):
    authentication_classes = [CombinedOnboardingAuthentication]
    permission_classes = (IsAuthenticated,)
    
    @extend_schema(
        tags=["Geography"],
        parameters=[
            OpenApiParameter(name="admin1_id", description="Filter by parent state (Admin1) UUID", required=True, type=OpenApiTypes.UUID),
            OpenApiParameter(name="name", description="Filter exactly by district name", required=False, type=OpenApiTypes.STR),
            OpenApiParameter(name="search", description="Partial search on name", required=False, type=OpenApiTypes.STR),
            OpenApiParameter(name="page", description="Page number", required=False, type=OpenApiTypes.INT),
            OpenApiParameter(name="page_size", description="Number of results per page", required=False, type=OpenApiTypes.INT),
        ],
        responses={200: Admin2DropdownSerializer(many=True), 400: None}
    )
    def get(self, request, *args, **kwargs):
        admin1_id = request.query_params.get('admin1_id')
        if not admin1_id:
            return Response({
                "status": False,
                "message": "admin1_id parameter is required",
                "errors": {"code": "VALIDATION_ERROR"}
            }, status=status.HTTP_400_BAD_REQUEST)
            
        queryset = Admin2.objects.filter(admin1_id=admin1_id)
        
        # specific NO_FURTHER_DATA logic
        if not queryset.exists():
            return Response({
                "status": False,
                "message": "No districts found for this state",
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
            serializer = Admin2DropdownSerializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)
            
        serializer = Admin2DropdownSerializer(queryset, many=True)
        return Response({
            "status": True,
            "message": "success",
            "data": serializer.data,
            "errors": None
        })
