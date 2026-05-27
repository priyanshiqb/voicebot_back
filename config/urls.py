from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularSwaggerView,
    SpectacularRedocView,
)

api_v1_patterns = [
    # Include user registration and JWT auth endpoints
    path("auth/", include("user.urls")),
]

urlpatterns = [
    path("admin/", admin.site.path if hasattr(admin.site, "path") else admin.site.urls),
    
    # API endpoints grouped under v1 namespace
    path("api/v1/", include((api_v1_patterns, "api-v1"))),
    
    # OpenAPI Schema endpoints for interactive documentation
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path(
        "api/redoc/",
        SpectacularRedocView.as_view(url_name="schema"),
        name="redoc",
    ),
]
