from django.urls import include, path
from rest_framework.routers import DefaultRouter

from core.views.campaign_runner import CampaignRunnerViewSet

router = DefaultRouter()
router.register(
    "campaign-runner", CampaignRunnerViewSet, basename="campaign-runner"
)

urlpatterns = [
    path("", include(router.urls)),
]
