from django.urls import path, include
from rest_framework.routers import DefaultRouter
from core.views.knowledge_base import KnowledgeBaseViewSet

router = DefaultRouter()
router.register("knowledge-base", KnowledgeBaseViewSet, basename="knowledge-base")

urlpatterns = [
    path("", include(router.urls)),
]
