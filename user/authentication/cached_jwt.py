from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.exceptions import AuthenticationFailed
from user.models.user_session import UserSession
import logging

logger = logging.getLogger(__name__)

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
            try:
                UserSession.validate_session_jti(jti)
            except UserSession.DoesNotExist:
                raise AuthenticationFailed("Session is invalid or logged out.")
                
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
