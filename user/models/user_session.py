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

    #: How long a *positive* verdict is trusted without re-reading the row.
    #: Deliberately short, and deliberately not the token's lifetime. A cached
    #: "active" is only as fresh as this window, so anything that deactivates a
    #: session **without** going through ``revoke()`` — an admin edit, a bulk
    #: update, a direct SQL fix — takes effect within this many seconds rather
    #: than immediately. Every in-code revocation path writes "revoked" to the
    #: same key and is therefore instant; this bounds the ones that do not.
    POSITIVE_CACHE_SECONDS = 60

    @classmethod
    def _positive_cache_seconds(cls):
        return int(
            getattr(settings, "SESSION_CACHE_TTL_SECONDS", cls.POSITIVE_CACHE_SECONDS)
        )

    @classmethod
    def validate_session_jti(cls, jti):
        """
        Confirm that an access token's session is still usable.

        **Reads the database only on a cache miss.** The previous version consulted
        the cache but then issued ``objects.get(...)`` unconditionally, so every
        authenticated request in the system cost one query against
        ``user_sessions``; the cache saved the ``last_used`` *write*, not the read.
        That single query set the ceiling on request throughput.

        Asymmetric TTLs, because the two answers have different risk:

        * **"revoked"** is cached for the token's remaining life. It cannot become
          valid again, so re-checking it is pure waste, and a rejected token stays
          cheap for as long as it exists.
        * **"active"** is cached for ``SESSION_CACHE_TTL_SECONDS`` (60s) only — see
          :attr:`POSITIVE_CACHE_SECONDS` for why it is not the token lifetime.

        Redis being unavailable degrades to a database read per request, which is
        the old behaviour: slower, never wrong.

        Args:
            jti (str): The JWT Access Token JTI claim.

        Returns:
            UserSession | None: the row when it had to be read; ``None`` when the
            cached verdict was enough. **Callers must treat "no exception" as the
            result** and not rely on the return value.

        Raises:
            cls.DoesNotExist: If the session is invalid, expired, or revoked.
        """
        cache_key = f"user_session:access:{jti}"

        status = None
        try:
            status = cache.get(cache_key)
        except Exception as cache_err:
            logger.warning(
                "Cache lookup failed during JTI validation, falling back to DB: %s",
                str(cache_err),
            )

        if status == "revoked":
            raise cls.DoesNotExist("Session is invalid or logged out")

        if status == "active":
            # The whole point: a valid, recently-checked session costs no query.
            # `last_used` is refreshed on the next miss, which lands at most
            # POSITIVE_CACHE_SECONDS from now — the same throttle the old
            # last_used cache key implemented, minus a round trip.
            return None

        now = timezone.now()

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

        session.update_last_used()

        # Never outlive the token itself: a key that survived it would keep
        # answering "active" for a credential that no longer exists.
        timeout = cls._positive_cache_seconds()
        if session.access_exp:
            remaining = int(session.access_exp.timestamp() - now.timestamp())
            timeout = min(timeout, max(0, remaining))

        if timeout > 0:
            try:
                cache.set(cache_key, "active", timeout=timeout)
            except Exception as cache_err:
                logger.warning("Failed to cache session active status: %s", str(cache_err))

        return session

    @classmethod
    def get_active_session(cls, jti):
        """Return the live ``UserSession`` for an access token, or raise.

        For callers that need the **row**, not just a verdict — the WebSocket
        handshake, which puts it in the connection scope so a consumer can address
        messages at one session.

        Separate from :meth:`validate_session_jti` on purpose. That one may answer
        from cache and return ``None``, which is right for an HTTP request checking
        "is this still valid?" thousands of times, and wrong here. A WebSocket
        handshake happens once per connection, so a guaranteed read costs nothing
        worth optimising away — whereas quietly handing back ``None`` would leave
        ``scope["session"]`` empty and break session-scoped messaging in a way that
        only shows up under a warm cache.

        The revoked-cache check is still honoured, so a logged-out token is refused
        without touching the database.
        """
        cache_key = f"user_session:access:{jti}"
        try:
            if cache.get(cache_key) == "revoked":
                raise cls.DoesNotExist("Session is invalid or logged out")
        except cls.DoesNotExist:
            raise
        except Exception as cache_err:
            logger.warning(
                "Cache lookup failed during session load, falling back to DB: %s",
                str(cache_err),
            )

        session = cls.objects.get(
            access_jti=jti, is_active=True, expires_at__gt=timezone.now()
        )
        session.update_last_used()
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
