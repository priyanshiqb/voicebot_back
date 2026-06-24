from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
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


try:
    from drf_spectacular.extensions import OpenApiAuthenticationExtension

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
