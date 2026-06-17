from django.db import models
from django.conf import settings
from utils.models.base_model_core import BaseModelCore
from django.core.cache import cache
from django.utils import timezone
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
import logging

logger = logging.getLogger(__name__)

class UserSession(BaseModelCore):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="user_sessions"
    )
    refresh_jti = models.CharField(max_length=255, db_index=True)
    access_jti = models.CharField(max_length=255, db_index=True, blank=True, null=True)
    access_iat = models.DateTimeField(blank=True, null=True)
    access_exp = models.DateTimeField(blank=True, null=True)
    refresh_iat = models.DateTimeField(blank=True, null=True)
    refresh_exp = models.DateTimeField(blank=True, null=True)
    device_name = models.CharField(max_length=255, blank=True, null=True)
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    expires_at = models.DateTimeField()
    last_used = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "user_sessions"
        verbose_name = "User Session"
        verbose_name_plural = "User Sessions"

    @classmethod
    def validate_session_jti(cls, jti):
        """
        Validates the JTI against cache and PostgreSQL.
        Handles Redis downtime gracefully.
        
        Args:
            jti (str): The JWT Access Token JTI claim.
            
        Returns:
            UserSession: The active UserSession object if valid.
            
        Raises:
            cls.DoesNotExist: If session is invalid, expired, or revoked.
        """
        cache_key = f"user_session:access:{jti}"
        cache_key_last_used = f"user_session:last_used:{jti}"
        
        status = None
        last_cached_used = None
        try:
            status = cache.get(cache_key)
            last_cached_used = cache.get(cache_key_last_used)
        except Exception as cache_err:
            logger.warning("Cache lookup failed during JTI validation, falling back to DB: %s", str(cache_err))
            
        if status == "revoked":
            raise cls.DoesNotExist("Session is invalid or logged out")
            
        now = timezone.now()
            
        # If cache hit (active) and last_used is within the last 60s,
        # we can fetch the session but skip the DB update!
        skip_db_update = False
        if status == "active" and last_cached_used:
            if (now.timestamp() - last_cached_used) < 60:
                skip_db_update = True
                
        try:
            session = cls.objects.get(
                access_jti=jti,
                is_active=True,
                expires_at__gt=now
            )
        except cls.DoesNotExist:
            try:
                fallback_timeout = settings.ACCESS_TOKEN_LIFETIME_SECONDS
                cache.set(cache_key, "revoked", timeout=fallback_timeout)
            except Exception as cache_err:
                logger.warning("Failed to cache revocation status: %s", str(cache_err))
            raise
            
        if not skip_db_update:
            session.update_last_used()
            
            # Calculate remaining seconds for the token, capping the cache TTL
            fallback_timeout = settings.ACCESS_TOKEN_LIFETIME_SECONDS
            
            if session.access_exp:
                remaining_seconds = int(session.access_exp.timestamp() - now.timestamp())
                cache_timeout = min(fallback_timeout, max(0, remaining_seconds))
            else:
                cache_timeout = fallback_timeout
                
            try:
                cache.set(cache_key, "active", timeout=cache_timeout)
                cache.set(cache_key_last_used, now.timestamp(), timeout=cache_timeout)
            except Exception as cache_err:
                logger.warning("Failed to cache session active status: %s", str(cache_err))
                
        return session

    def revoke(self):
        """
        Deactivate the session and register its access token as blacklisted in Redis.
        """
        self.is_active = False
        self.save(update_fields=["is_active"])
        
        # Invalidate access token from cache
        if self.access_jti:
            timeout = settings.ACCESS_TOKEN_LIFETIME_SECONDS
            if self.access_exp:
                remaining = int(self.access_exp.timestamp() - timezone.now().timestamp())
                timeout = max(60, remaining)  # Cache for at least 60s or exact remaining time
                
            cache.set(f"user_session:access:{self.access_jti}", "revoked", timeout=timeout)

    def update_last_used(self):
        """
        Updates the last used timestamp. Throttles updates to once per 60 seconds
        to prevent database write bottleneck.
        """
        now = timezone.now()
        # Only update if the last used timestamp was more than 60 seconds ago
        if not self.last_used or (now - self.last_used).total_seconds() > 60:
            self.last_used = now
            self.save(update_fields=["last_used"])

    def send_ws_message_to_user(self, event_type, message, data=None):
        """
        Sends a WebSocket message to ALL sessions and tabs belonging to this User.
        """
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
