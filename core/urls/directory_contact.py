from django.urls import include, path
from rest_framework.routers import DefaultRouter

from core.views.directory_contact import DirectoryContactViewSet

router = DefaultRouter()
router.register(
    "directory-contacts", DirectoryContactViewSet, basename="directory-contact"
)

urlpatterns = [
    path("", include(router.urls)),
]
