from two_factor.admin import AdminSiteOTPRequired
from django.contrib import admin
from django.urls import path, include
from two_factor.urls import urlpatterns as tf_urls

admin.site.__class__ = AdminSiteOTPRequired
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularSwaggerView,
    SpectacularRedocView,
)

# Import for serving media and static files during development
from django.conf import settings
from django.conf.urls.static import static

api_v1_patterns = [
    # Include user registration and JWT auth endpoints
    path("auth/", include("user.urls")),
    path("onboard/", include("onboard.urls")),
    path("core/", include("core.urls")),
]

urlpatterns = [
    path("", include(tf_urls)),
    path('admin/defender/', include('defender.urls')),
    path("admin/", admin.site.urls),
    path("prom-metrics/", include("django_prometheus.urls")),
    
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

# Add media and static file serving for local development
if settings.DEBUG:
    if not getattr(settings, "USE_S3_STORAGE", False):
        urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)  