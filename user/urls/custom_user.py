from django.urls import path, include
from rest_framework.routers import DefaultRouter
from user.views.custom_user import CustomUserViewSet

router = DefaultRouter()
router.register(r'users', CustomUserViewSet, basename='customuser')

urlpatterns = [
    path('', include(router.urls)),
]