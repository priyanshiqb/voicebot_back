from django.urls import include, path
from rest_framework.routers import DefaultRouter

from core.views.availability_profile import AvailabilityProfileViewSet

router = DefaultRouter()
router.register(
    "availability-profiles", AvailabilityProfileViewSet, basename="availability-profile"
)

urlpatterns = [
    path("", include(router.urls)),
]
