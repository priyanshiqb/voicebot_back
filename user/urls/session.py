from django.urls import path, include
from rest_framework.routers import DefaultRouter
from user.views.session import UserSessionViewSet

router = DefaultRouter()
router.register(r'sessions', UserSessionViewSet, basename='usersession')

urlpatterns = [
    path('', include(router.urls)),
]
