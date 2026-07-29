from django.urls import include, path
from rest_framework.routers import DefaultRouter

from core.views.contact_import import ContactImportRowViewSet, ContactImportViewSet

router = DefaultRouter()
router.register("contact-imports", ContactImportViewSet, basename="contact-import")
router.register(
    "contact-import-rows", ContactImportRowViewSet, basename="contact-import-row"
)

urlpatterns = [
    path("", include(router.urls)),
]
