from django.urls import path, include
from rest_framework.routers import DefaultRouter
from core.views.probing_question import ProbingQuestionViewSet

router = DefaultRouter()
router.register("probing-questions", ProbingQuestionViewSet, basename="probing-question")

urlpatterns = [
    path("", include(router.urls)),
]
