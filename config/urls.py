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
from rest_framework.permissions import AllowAny


def docs_patterns(prefix, name_prefix="", **view_kwargs):
    """Schema, Swagger UI and ReDoc under one prefix.

    Each set points at its own schema route by name: the UI is a page that fetches the
    document over HTTP, so a protected UI pointed at a public schema would protect
    nothing, and a public UI pointed at a protected one would show an error.
    """
    schema_name = f"{name_prefix}schema"
    return [
        path(
            f"{prefix}schema/",
            SpectacularAPIView.as_view(**view_kwargs),
            name=schema_name,
        ),
        path(
            f"{prefix}docs/",
            SpectacularSwaggerView.as_view(url_name=schema_name, **view_kwargs),
            name=f"{name_prefix}swagger-ui",
        ),
        path(
            f"{prefix}redoc/",
            SpectacularRedocView.as_view(url_name=schema_name, **view_kwargs),
            name=f"{name_prefix}redoc",
        ),
    ]

# Import for serving media and static files during development
from django.conf import settings
from django.conf.urls.static import static

api_v1_patterns = [
    # Include user registration and JWT auth endpoints
    path("auth/", include("user.urls")),
    path("onboard/", include("onboard.urls")),
    path("core/", include("core.urls")),
    path("tenant/", include("tenant.urls")),
    path("billing/", include("wallet.urls")),
    path("billing/", include("onboard.urls.billing")),
    # The admin console. Gated as a whole by IsPlatformStaff -- a tenant user, however
    # privileged inside their own tenant, cannot reach anything under here.
    path("ops/", include("ops.urls")),
]

from django.http import JsonResponse

def health_check(request):
    return JsonResponse({"status": "healthy"})

urlpatterns = [
    path("health/", health_check, name="health_check"),
    path("", include(tf_urls)),
    path('admin/defender/', include('defender.urls')),
    path("admin/", admin.site.urls),
    path("prom-metrics/", include("django_prometheus.urls")),
    
    # API endpoints grouped under v1 namespace
    path("api/v1/", include((api_v1_patterns, "api-v1"))),
    
]

# The documentation, for the people who are meant to read it. Registered everywhere and
# closed everywhere: SPECTACULAR_SETTINGS requires an account holding
# "Can view the API documentation", which is granted per person. Sign in to /admin/
# first — that is what gives a browser the session cookie these read.
urlpatterns += docs_patterns("api/internal/", "internal-")

# The familiar unauthenticated paths. Only where a deployment asks for them, which
# defaults to DEBUG, because this hands the whole API map to anyone who can reach the
# host. Never turn this on where that is the internet.
if settings.API_DOCS_ENABLED:
    urlpatterns += docs_patterns(
        "api/", permission_classes=[AllowAny], authentication_classes=[]
    )

# Add media and static file serving for local development
if settings.DEBUG:
    if not getattr(settings, "USE_AZURE_STORAGE", False):
        urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)  