"""Pluggable external-calendar providers.

Google is currently the only one. The registry and `CalendarProvider` interface are
kept so adding another provider stays an addition rather than a refactor.
"""

from core.services.calendars.base import BusyInterval, CalendarProvider, OAuthTokens
from core.services.calendars.google import GoogleCalendarProvider

_PROVIDERS = {
    GoogleCalendarProvider.name: GoogleCalendarProvider,
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
    "get_provider",
    "get_provider_for",
    "get_provider_class",
]
