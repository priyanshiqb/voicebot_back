from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.exceptions import AuthenticationFailed
from django.utils import timezone
from user.models.user_session import UserSession

class CachedJWTAuthentication(JWTAuthentication):
    """
    Subclass of JWTAuthentication to verify that the access token JTI
    is still registered as active and not revoked in UserSession.
    Uses Redis cache caching to avoid database queries on every API call.
    """
    def get_validated_token(self, raw_token):
        validated_token = super().get_validated_token(raw_token)
        
        jti = validated_token.get("jti")
        if jti:
            from django.core.cache import cache
            
            cache_key = f"user_session:access:{jti}"
            cache_key_last_used = f"user_session:last_used:{jti}"
            
            status = cache.get(cache_key)
            last_cached_used = cache.get(cache_key_last_used)
            now = timezone.now()
            
            if status == "revoked":
                raise AuthenticationFailed("Session is invalid or logged out.")
            
            if status == "active" and last_cached_used:
                # Throttled cache hit: skip DB access if updated within the last 60 seconds
                if (now.timestamp() - last_cached_used) < 60:
                    return validated_token
            
            # Fallback/Update from DB
            try:
                session = UserSession.objects.get(
                    access_jti=jti,
                    is_active=True,
                    expires_at__gt=now
                )
            except UserSession.DoesNotExist:
                cache.set(cache_key, "revoked", timeout=300)
                raise AuthenticationFailed("Session is invalid or logged out.")
            
            # Update last used in database (throttled to once per 60 seconds)
            session.update_last_used()
            
            # Cache active status and current last_used timestamp
            cache.set(cache_key, "active", timeout=300)
            cache.set(cache_key_last_used, now.timestamp(), timeout=300)
                
        return validated_token


try:
    from drf_spectacular.extensions import OpenApiAuthenticationExtension

    class CachedJWTAuthenticationScheme(OpenApiAuthenticationExtension):
        target_class = "user.authentication.CachedJWTAuthentication"
        name = "jwt"

        def get_security_definition(self, auto_schema):
            return {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
            }
except ImportError:
    pass
