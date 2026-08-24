from django.db import models
from django.utils.translation import gettext_lazy as _


class ServiceTier(models.TextChoices):
    """How a call is served — and therefore what a minute of it costs.

    A bot declares its tier (``Agent.service_tier``); the runner reports the tier a
    call actually ran on (``AgentCallLog.service_tier``). Billing reads the call log
    and falls back to the agent, so a runtime downgrade — low latency unavailable,
    call served on the standard model — is charged at what really happened rather
    than at what was configured.

    **These values are deliberately identical to the matching**
    ``wallet.CreditRate.Operation`` **members.** The two enums are not merged because
    their sets differ: ``Operation`` also prices things that are not call tiers at
    all, such as storage. Keeping the *strings* equal makes the mapping in
    ``wallet.services.policy`` an identity, and a test asserts every tier here has a
    priceable operation there — so the two cannot drift apart silently.
    """

    STATIC_REEL = "static_reel", _("Static Reel (one-way playback)")
    CONVERSATION_HIGH_LATENCY = (
        "conversation_high_latency",
        _("Conversation — Standard (high latency)"),
    )
    CONVERSATION_LOW_LATENCY = (
        "conversation_low_latency",
        _("Conversation — Realtime (low latency)"),
    )
