from django.urls import include, path
from rest_framework.routers import DefaultRouter

from core.views.event_template import EventTemplateViewSet

router = DefaultRouter()
router.register("event-templates", EventTemplateViewSet, basename="event-template")

urlpatterns = [
    path("", include(router.urls)),
]
