from django.urls import path, include
from rest_framework.routers import DefaultRouter
from core.views.agent_call_log import AgentCallLogViewSet

router = DefaultRouter()
router.register("agent-call-logs", AgentCallLogViewSet, basename="agent-call-log")

urlpatterns = [
    path("", include(router.urls)),
]
