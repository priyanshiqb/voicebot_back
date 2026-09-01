from django.urls import include, path
from rest_framework.routers import DefaultRouter

from user.views.invitation import TenantInvitationViewSet

router = DefaultRouter()
router.register(r"invitations", TenantInvitationViewSet, basename="invitation")

urlpatterns = [
    path("", include(router.urls)),
]
