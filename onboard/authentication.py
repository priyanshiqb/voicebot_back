from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from django.core import signing
from django.conf import settings
from onboard.models import TempUser
from user.authentication import CachedJWTAuthentication


class TempUserAuthentication(BaseAuthentication):
    """
    Custom DRF Authentication backend to validate short-lived temporary scoped tokens.
    It returns the authenticated TempUser instance as the request.user.
    """

    def authenticate(self, request):
        auth_header = request.headers.get("Authorization")
        if not auth_header:
            return None

        parts = auth_header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None

        token = parts[1]
        try:
            # Cryptographically load and verify the signature with a 7-days expiration
            data = signing.loads(token, key=settings.SECRET_KEY, max_age=3600*24*7)
            temp_user_id = data.get("temp_user_id")
            if not temp_user_id:
                return None

            temp_user = TempUser.objects.get(id=temp_user_id)
            # Django REST framework expects (user, auth)
            return (temp_user, token)
        except signing.SignatureExpired:
            raise AuthenticationFailed("Temporary token has expired")
        except (signing.BadSignature, TempUser.DoesNotExist):
            raise AuthenticationFailed("Invalid temporary token")


def generate_temp_token(temp_user):
    """
    Utility to cryptographically sign a temporary scoped onboarding token.
    """
    return signing.dumps({"temp_user_id": str(temp_user.id)}, key=settings.SECRET_KEY)


try:
    from drf_spectacular.extensions import OpenApiAuthenticationExtension

    class TempUserAuthenticationScheme(OpenApiAuthenticationExtension):
        target_class = "onboard.authentication.TempUserAuthentication"
        name = "TempUserTokenAuth"

        def get_security_definition(self, auto_schema):
            return {
                "type": "apiKey",
                "in": "header",
                "name": "Authorization",
                "description": "Scoped temporary onboarding token prefixed with 'Bearer '"
            }

    class CombinedOnboardingAuthenticationScheme(OpenApiAuthenticationExtension):
        target_class = "onboard.authentication.CombinedOnboardingAuthentication"
        name = "CombinedOnboardingAuth"

        def get_security_definition(self, auto_schema):
            # This is a combined scheme that delegates to JWT and TempUserTokenAuth.
            # We return an empty dict since it references the target security definitions.
            return {}

        def get_security_requirement(self, auto_schema):
            return [
                {"jwt": []},
                {"TempUserTokenAuth": []}
            ]
except ImportError:
    pass

class CombinedOnboardingAuthentication(BaseAuthentication):
    """
    Combined authentication class that tries CachedJWTAuthentication first.
    If that fails, it falls back to TempUserAuthentication.
    Only if both fail does it raise an AuthenticationFailed exception.
    """
    def authenticate(self, request):
        jwt_auth = CachedJWTAuthentication()
        temp_auth = TempUserAuthentication()

        # Try CachedJWTAuthentication
        try:
            jwt_res = jwt_auth.authenticate(request)
            if jwt_res is not None:
                return jwt_res
        except AuthenticationFailed:
            # Ignore JWT failures and try TempUserAuthentication
            pass

        # Try TempUserAuthentication
        try:
            temp_res = temp_auth.authenticate(request)
            if temp_res is not None:
                return temp_res
        except AuthenticationFailed:
            # If temp user auth failed with an explicit error, bubble it up
            raise

        return None

    def authenticate_header(self, request):
        return "Bearer"

