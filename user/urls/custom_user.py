from django.urls import path, include
from rest_framework.routers import DefaultRouter
from user.views.custom_user import CustomUserViewSet

router = DefaultRouter()
router.register(r'', CustomUserViewSet, basename='customuser')

urlpatterns = [
    path('custom-users/', include(router.urls)),
]