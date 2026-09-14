from django.urls import include, path
from rest_framework.routers import DefaultRouter

from core.views.campaign_report import CampaignReportViewSet

router = DefaultRouter()
router.register("campaign-reports", CampaignReportViewSet, basename="campaign-report")

urlpatterns = [
    path("", include(router.urls)),
]
