"""Plan configuration routes."""

from rest_framework.routers import DefaultRouter

from ops.views import PlanConfigurationViewSet

router = DefaultRouter()
router.register(r"plans", PlanConfigurationViewSet, basename="ops-plan")

urlpatterns = router.urls
