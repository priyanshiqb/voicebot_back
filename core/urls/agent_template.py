from django.urls import path, include
from rest_framework.routers import DefaultRouter
from core.views.agent_template import AgentTemplateViewSet

router = DefaultRouter()
router.register("agent-templates", AgentTemplateViewSet, basename="agent-template")

urlpatterns = [
    path("", include(router.urls)),
]
