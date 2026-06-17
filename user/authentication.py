from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.exceptions import AuthenticationFailed
from django.utils import timezone
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

    class ETLSystemTokenAuthenticationScheme(OpenApiAuthenticationExtension):
        target_class = "user.authentication.ETLSystemTokenAuthentication"
        name = "etlToken"

        def get_security_definition(self, auto_schema):
            return {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "Token",
            }
except ImportError:
    pass


from rest_framework.authentication import BaseAuthentication
from django.conf import settings
from user.models.CustomUser import CustomUser

class ETLSystemTokenAuthentication(BaseAuthentication):
    """
    Authentication backend that validates requests from the ETL worker
    using a static bearer token configured in the environment settings.
    """
    def authenticate(self, request):
        auth_header = request.META.get("HTTP_AUTHORIZATION")
        if not auth_header:
            return None

        try:
            auth_type, token = auth_header.split()
            if auth_type.lower() != "bearer":
                return None
        except ValueError:
            return None

        expected_token = getattr(settings, "ETL_SYSTEM_TOKEN", None)
        if not expected_token or token != expected_token:
            raise AuthenticationFailed("Invalid ETL system token.")

        try:
            etl_user = CustomUser.objects.get(user_type=CustomUser.UserType.ETL, is_active=True)
            return (etl_user, None)
        except CustomUser.DoesNotExist:
            raise AuthenticationFailed("System ETL user not found in database.")

