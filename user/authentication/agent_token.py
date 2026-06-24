from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from django.utils import timezone
from django.contrib.auth.hashers import check_password
from user.models.CustomUser import CustomUser

class AgentTokenAuthentication(BaseAuthentication):
    """
    Authentication backend for Voicebot Agents.
    Validates a generated AgentToken provided in the Authorization header.
    Expects format: `prefix_secret`
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

        # Token should be in format `vb_prefix_secret`
        parts = token.split('_')
        if len(parts) < 3 or parts[0] != 'vb':
            return None  # This is likely a JWT or other token. Pass it to the next auth class.
            
        prefix = f"{parts[0]}_{parts[1]}"
        secret = '_'.join(parts[2:])

        from user.models.agent_token import AgentToken

        try:
            agent_token = AgentToken.objects.select_related('user').get(prefix=prefix, is_active=True)
        except AgentToken.DoesNotExist:
            raise AuthenticationFailed("Invalid or inactive agent token.")
            
        # Verify the secret against the hashed key in the database
        if not check_password(secret, agent_token.key):
            raise AuthenticationFailed("Invalid agent token secret.")

        if agent_token.expires_at and agent_token.expires_at < timezone.now():
            raise AuthenticationFailed("Agent token has expired.")

        user = agent_token.user
        if not user.is_active or user.user_type != CustomUser.UserType.VB_AGENT:
            raise AuthenticationFailed("User is inactive or not an agent.")

        # Update last_used_at timestamp
        agent_token.last_used_at = timezone.now()
        agent_token.save(update_fields=['last_used_at'])

        return (user, agent_token)

    def authenticate_header(self, request):
        return 'Bearer'


try:
    from drf_spectacular.extensions import OpenApiAuthenticationExtension

    class AgentTokenAuthenticationScheme(OpenApiAuthenticationExtension):
        target_class = "user.authentication.AgentTokenAuthentication"
        name = "agentToken"

        def get_security_definition(self, auto_schema):
            return {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "Token",
                "description": "Agent token authentication for voicebot integrations.",
            }
except ImportError:
    pass
