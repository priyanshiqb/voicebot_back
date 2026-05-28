from django.urls import path, include
from rest_framework.routers import DefaultRouter
from onboard.views.plan import PlanViewSet

router = DefaultRouter()
router.register(r'', PlanViewSet, basename='plan')

urlpatterns = [
    path('plans/', include(router.urls)),
]
