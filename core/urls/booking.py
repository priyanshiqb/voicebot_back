from django.urls import include, path
from rest_framework.routers import DefaultRouter

from core.views.booking import BookingViewSet

router = DefaultRouter()
router.register("bookings", BookingViewSet, basename="booking")

urlpatterns = [
    path("", include(router.urls)),
]
