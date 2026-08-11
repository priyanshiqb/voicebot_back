from django.urls import include, path
from rest_framework.routers import DefaultRouter

from core.views.catalog_category import CatalogCategoryViewSet
from core.views.catalog_item import CatalogItemViewSet
from core.views.catalog_item_media import CatalogItemMediaViewSet
from core.views.catalog_variant import CatalogVariantViewSet

# One module for the four resources because they are one product surface, and a
# reader looking for "where is the catalog routed" should find it in one file.
# Registrations are flat — `drf-nested-routers` is not a dependency, and every other
# url module here is a flat DefaultRouter.
router = DefaultRouter()
router.register(
    "catalog-categories", CatalogCategoryViewSet, basename="catalog-category"
)
router.register("catalog-items", CatalogItemViewSet, basename="catalog-item")
router.register("catalog-variants", CatalogVariantViewSet, basename="catalog-variant")
router.register(
    "catalog-item-media", CatalogItemMediaViewSet, basename="catalog-item-media"
)

urlpatterns = [
    path("", include(router.urls)),
]
