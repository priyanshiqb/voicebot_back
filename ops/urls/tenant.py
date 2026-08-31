"""Tenant console routes."""

from rest_framework.routers import DefaultRouter

from ops.views import TenantConsoleViewSet

router = DefaultRouter()
router.register(r"tenants", TenantConsoleViewSet, basename="ops-tenant")

urlpatterns = router.urls
