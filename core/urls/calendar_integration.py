from django.urls import include, path
from rest_framework.routers import DefaultRouter

from core.views.calendar_integration import CalendarIntegrationViewSet

router = DefaultRouter()
router.register(
    "calendar-integrations", CalendarIntegrationViewSet, basename="calendar-integration"
)

urlpatterns = [
    # Explicit slash-less route to prevent 500 RuntimeError on POST requests when APPEND_SLASH=True
    path(
        "calendar-integrations/callback",
        CalendarIntegrationViewSet.as_view({"get": "callback_slashless"}),
    ),
    path("", include(router.urls)),
]
