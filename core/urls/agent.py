from django.urls import path, include
from rest_framework.routers import DefaultRouter
from core.views.agent import AgentViewSet

router = DefaultRouter()
router.register("agents", AgentViewSet, basename="agent")

urlpatterns = [
    path("", include(router.urls)),
]
