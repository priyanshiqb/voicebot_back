from django.urls import include, path
from rest_framework.routers import DefaultRouter

from core.views.availability_exception import AvailabilityExceptionViewSet

router = DefaultRouter()
router.register(
    "availability-exceptions",
    AvailabilityExceptionViewSet,
    basename="availability-exception",
)

urlpatterns = [
    path("", include(router.urls)),
]
