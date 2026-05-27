import os
import django
from django.core.asgi import get_asgi_application

# Initialize Django registry before importing channels components to prevent AppRegistryNotReady
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
django.setup()

from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from utils.middleware.websocket_auth import JWTAuthMiddleware
from tenant.urls.websocket_routing import websocket_urlpatterns as tenant_websocket_urlpatterns

# Unified app-wide WebSocket routes
websocket_urlpatterns = [
    *tenant_websocket_urlpatterns,
]

application = ProtocolTypeRouter({
    # 1. Standard HTTP requests routed to Django API handlers
    "http": get_asgi_application(),

    # 2. WebSockets routed to authenticated consumer handlers
    "websocket": JWTAuthMiddleware(
        AuthMiddlewareStack(
            URLRouter(websocket_urlpatterns)
        )
    ),
})
