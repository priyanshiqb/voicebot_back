"""Pluggable external-calendar providers (Google + Microsoft/Outlook)."""

from core.services.calendars.base import BusyInterval, CalendarProvider, OAuthTokens
from core.services.calendars.google import GoogleCalendarProvider
from core.services.calendars.microsoft import MicrosoftCalendarProvider

_PROVIDERS = {
    GoogleCalendarProvider.name: GoogleCalendarProvider,
    MicrosoftCalendarProvider.name: MicrosoftCalendarProvider,
}


def get_provider(provider_name):
    cls = _PROVIDERS.get(provider_name)
    if cls is None:
        raise ValueError(f"Unsupported calendar provider: {provider_name}")
    return cls()


def get_provider_for(integration):
    return get_provider(integration.provider)


def get_provider_class(provider_name):
    """Return the provider *class* (for classmethod OAuth calls: authorization_url,
    exchange_code) without instantiating."""
    cls = _PROVIDERS.get(provider_name)
    if cls is None:
        raise ValueError(f"Unsupported calendar provider: {provider_name}")
    return cls


__all__ = [
    "BusyInterval",
    "CalendarProvider",
    "OAuthTokens",
    "GoogleCalendarProvider",
    "MicrosoftCalendarProvider",
    "get_provider",
    "get_provider_for",
    "get_provider_class",
]
