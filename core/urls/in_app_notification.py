from django.urls import path, include
from rest_framework.routers import DefaultRouter
from core.views.in_app_notification import InAppNotificationViewSet

router = DefaultRouter()
router.register(r"in-app-notifications", InAppNotificationViewSet, basename="in_app_notification")

urlpatterns = [
    path("", include(router.urls)),
]
