from django.urls import path, include
from rest_framework.routers import DefaultRouter
from onboard.views.language import LanguageViewSet

router = DefaultRouter()
router.register(r'', LanguageViewSet, basename='language')

urlpatterns = [
    path('languages/', include(router.urls)),
]
