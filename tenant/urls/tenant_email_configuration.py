from django.urls import include, path
from rest_framework.routers import DefaultRouter

from tenant.views.tenant_email_configuration import TenantEmailConfigurationViewSet

router = DefaultRouter()
router.register(
    "email-configurations",
    TenantEmailConfigurationViewSet,
    basename="tenant-email-configuration",
)

urlpatterns = [
    path("", include(router.urls)),
]
