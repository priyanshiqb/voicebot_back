from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from onboard.models import Place
from onboard.serializers.geography.place import PlaceSerializer
from drf_spectacular.utils import extend_schema
from onboard.authentication import CombinedOnboardingAuthentication
from .mixins import GeographyTimeoutMixin

class PlaceDetailAPIView(GeographyTimeoutMixin, APIView):
    authentication_classes = [CombinedOnboardingAuthentication]
    permission_classes = (IsAuthenticated,)

    @extend_schema(
        tags=["Geography"],
        responses={200: PlaceSerializer}
    )
    def get(self, request, pk, *args, **kwargs):
        # Using select_related to optimize FK fetches
        queryset = Place.objects.select_related('country', 'admin1', 'admin2', 'admin3')
        place = get_object_or_404(queryset, pk=pk)
        
        serializer = PlaceSerializer(place, context={'request': request})
        return Response({
            "status": True,
            "message": "success",
            "data": serializer.data,
            "errors": None
        })
