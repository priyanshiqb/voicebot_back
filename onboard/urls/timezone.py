from django.urls import path, include
from rest_framework.routers import DefaultRouter
from onboard.views.timezone import TimezoneViewSet

router = DefaultRouter()
router.register(r'', TimezoneViewSet, basename='timezone')

urlpatterns = [
    path('timezones/', include(router.urls)),
]
