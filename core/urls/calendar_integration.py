from django.urls import include, path
from rest_framework.routers import DefaultRouter

from core.views.calendar_integration import CalendarIntegrationViewSet

router = DefaultRouter()
router.register(
    "calendar-integrations", CalendarIntegrationViewSet, basename="calendar-integration"
)

urlpatterns = [
    path("", include(router.urls)),
]
