from django.urls import include, path
from rest_framework.routers import DefaultRouter

from core.views.user_event_skill import UserEventSkillViewSet

router = DefaultRouter()
router.register("event-skills", UserEventSkillViewSet, basename="event-skill")

urlpatterns = [
    path("", include(router.urls)),
]
