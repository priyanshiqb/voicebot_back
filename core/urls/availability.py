from django.urls import include, path
from rest_framework.routers import DefaultRouter

from core.views.availability import AvailabilityViewSet

router = DefaultRouter()
router.register("availability", AvailabilityViewSet, basename="availability")

urlpatterns = [
    path("", include(router.urls)),
]
