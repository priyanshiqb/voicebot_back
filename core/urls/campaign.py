from django.urls import include, path
from rest_framework.routers import DefaultRouter

from core.views.campaign import CampaignViewSet

router = DefaultRouter()
router.register("campaigns", CampaignViewSet, basename="campaign")

urlpatterns = [
    path("", include(router.urls)),
]
