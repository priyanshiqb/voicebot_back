from django.db import models
from django.conf import settings
from utils.models.base_model_core import BaseModelCore

class UserSession(BaseModelCore):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="user_sessions"
    )
    refresh_jti = models.CharField(max_length=255, db_index=True)
    access_jti = models.CharField(max_length=255, db_index=True, blank=True, null=True)
    device_name = models.CharField(max_length=255, blank=True, null=True)
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    expires_at = models.DateTimeField()
    last_used = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "user_sessions"
        verbose_name = "User Session"
        verbose_name_plural = "User Sessions"

    def revoke(self):
        """
        Deactivate the session and register its access token as blacklisted in Redis.
        """
        self.is_active = False
        self.save(update_fields=["is_active"])
        
        # Invalidate access token from cache
        if self.access_jti:
            from django.core.cache import cache
            cache.set(f"user_session:access:{self.access_jti}", "revoked", timeout=300)

    def update_last_used(self):
        """
        Updates the last used timestamp. Throttles updates to once per 60 seconds
        to prevent database write bottleneck.
        """
        from django.utils import timezone
        now = timezone.now()
        # Only update if the last used timestamp was more than 60 seconds ago
        if not self.last_used or (now - self.last_used).total_seconds() > 60:
            self.last_used = now
            self.save(update_fields=["last_used"])

    def send_ws_message_to_user(self, event_type, message, data=None):
        """
        Sends a WebSocket message to ALL sessions and tabs belonging to this User.
        """
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
            
        payload = {
            "type": "send_update",
            "event_type": event_type,
            "message": message,
            "data": data or {}
        }
        async_to_sync(channel_layer.group_send)(f"user_{self.user_id}", payload)

    def send_ws_message_to_session(self, event_type, message, data=None):
        """
        Sends a WebSocket message to all active tabs connected under THIS specific session.
        """
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
            
        payload = {
            "type": "send_update",
            "event_type": event_type,
            "message": message,
            "data": data or {}
        }
        async_to_sync(channel_layer.group_send)(f"session_{self.id}", payload)

    def send_ws_message_to_one_tab(self, event_type, message, data=None):
        """
        Sends a message to exactly one connected tab in this session.
        Because Channels broadcasts to all members in a group, this method broadcasts to the session 
        but includes a metadata flag (`_target_single_tab`) instructing the frontend leader-election 
        logic to process it exactly once (e.g. for notification sounds).
        """
        data = data or {}
        data["_target_single_tab"] = True
        
        self.send_ws_message_to_session(event_type, message, data)
