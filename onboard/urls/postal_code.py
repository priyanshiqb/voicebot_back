from django.urls import path, include
from rest_framework.routers import DefaultRouter
from onboard.views.postal_code import PostalCodeViewSet

router = DefaultRouter()
router.register(r'', PostalCodeViewSet, basename='postal-code')

urlpatterns = [
    path('postal-codes/', include(router.urls)),
]
