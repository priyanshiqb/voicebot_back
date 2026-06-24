from django.db import models
from django.conf import settings
from utils.models.base_model_core import BaseModelCore
from django.utils.translation import gettext_lazy as _
import secrets

class AgentToken(BaseModelCore):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='agent_tokens',
        help_text=_("The voicebot agent user this token belongs to.")
    )
    name = models.CharField(
        max_length=150,
        help_text=_("A descriptive name for this token (e.g. 'Production Agent').")
    )
    prefix = models.CharField(
        max_length=20,
        unique=True,
        help_text=_("The plain-text prefix of the token used for lookup.")
    )
    key = models.CharField(
        max_length=128,
        help_text=_("The cryptographically hashed secret key.")
    )
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Optional expiration date. If left blank and 'Never expires' is unchecked, defaults to 1 year.")
    )
    never_expires = models.BooleanField(
        default=False,
        help_text=_("If checked, this token will never expire. Overrides any 'expires_at' value.")
    )
    last_used_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Timestamp of when this token was last used to authenticate.")
    )

    class Meta:
        db_table = 'user_agent_tokens'
        verbose_name = _("Agent Token")
        verbose_name_plural = _("Agent Tokens")

    def __str__(self):
        return f"{self.name} - {self.user.email}"
