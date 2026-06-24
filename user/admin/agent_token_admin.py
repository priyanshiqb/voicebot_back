from django.contrib import admin, messages
from django.utils.safestring import mark_safe
from django.contrib.auth.hashers import make_password
from django.utils import timezone
from datetime import timedelta
from utils.admin.base_admin_core import BaseAdminCore
from user.models.agent_token import AgentToken
import secrets

@admin.register(AgentToken)
class AgentTokenAdmin(BaseAdminCore):
    list_display = (
        "name",
        "user",
        "prefix",
        "expires_at",
        "never_expires",
        "last_used_at",
        "is_active",
        "created_at",
    )
    list_filter = ("is_active", "never_expires", "user")
    search_fields = ("name", "prefix", "user__email", "id")
    autocomplete_fields = ("user",)
    readonly_fields = ("prefix", "key", "created_at", "updated_at", "last_used_at")
    
    def get_exclude(self, request, obj=None):
        if not obj: # creating a new token
            return ("prefix", "key")
        return ("key",)

    def get_readonly_fields(self, request, obj=None):
        return self.readonly_fields

    def save_model(self, request, obj, form, change):
        # Enforce expiration logic
        if obj.never_expires:
            obj.expires_at = None
        elif not obj.expires_at:
            # Default to exactly 1 year from now if left blank
            obj.expires_at = timezone.now() + timedelta(days=365)

        if not change:
            # Generating a new token
            raw_prefix = f"vb_{secrets.token_hex(4)}"
            raw_secret = secrets.token_hex(40)
            
            # Store plain text prefix and hashed secret
            obj.prefix = raw_prefix
            obj.key = make_password(raw_secret)
            
            # Save the object first
            super().save_model(request, obj, form, change)
            
            # Display the raw token to the user exactly once
            full_raw_token = f"{raw_prefix}_{raw_secret}"
            messages.success(
                request,
                f"CRITICAL: Your new Voicebot Agent Token is generated! "
                f"Please copy it now, as it will NEVER be displayed again:  {full_raw_token}"
            )
        else:
            super().save_model(request, obj, form, change)
