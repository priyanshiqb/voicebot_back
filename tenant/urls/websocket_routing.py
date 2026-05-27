from django.urls import re_path

# Tenant-specific WebSocket URL patterns (register voicebot consumers here!)
websocket_urlpatterns = [
    # E.g. re_path(r"ws/calls/(?P<tenant_id>[a-f0-9-]+)/$", CallConsumer.as_asgi()),
]
