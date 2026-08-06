# Tenant URLs package initialization
from django.urls import include, path

app_name = "tenant"

# Consolidated tenant app REST patterns. Websocket routing lives in
# `websocket_routing.py` and is wired through the ASGI application, not here.
urlpatterns = [
    path("", include("tenant.urls.tenant_email_configuration")),
]
