"""Provider-agnostic calendar interface. Concrete providers (e.g. Google) keep
their SDK imports lazy so this package loads — and tests run — without the
provider libraries installed or reachable."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class BusyInterval:
    start: datetime  # UTC-aware
    end: datetime  # UTC-aware


@dataclass(frozen=True)
class OAuthTokens:
    access_token: str
    refresh_token: str
    token_expires_at: Optional[datetime]
    scope: str
    account_email: str


@dataclass(frozen=True)
class WatchChannel:
    channel_id: str
    resource_id: str
    expiration: Optional[datetime]


@dataclass(frozen=True)
class ChangedEvent:
    external_id: str
    cancelled: bool
    start: Optional[datetime]  # None when cancelled/all-day-unparseable
    end: Optional[datetime]


@dataclass(frozen=True)
class SyncResult:
    changes: list  # list[ChangedEvent]
    sync_token: str


class CalendarProvider:
    """Interface every provider implements. `name` matches
    CalendarIntegration.Provider values."""

    name = None

    # --- OAuth (config-level; no integration yet) ----------------------------
    @classmethod
    def authorization_url(cls, state):
        raise NotImplementedError

    @classmethod
    def exchange_code(cls, code):
        """Exchange an auth code for OAuthTokens."""
        raise NotImplementedError

    # --- per-integration operations ------------------------------------------
    def get_busy(self, integration, window_start, window_end):
        """Return the account's busy intervals in the window (list[BusyInterval])."""
        raise NotImplementedError

    def push_event(self, integration, booking):
        """Create the mirrored event; return its external id."""
        raise NotImplementedError

    def update_event(self, integration, booking):
        raise NotImplementedError

    def delete_event(self, integration, external_event_id):
        raise NotImplementedError

    def refresh_access_token(self, integration):
        """Refresh the access token in-place on the integration (not saved)."""
        raise NotImplementedError

    def revoke(self, integration):
        raise NotImplementedError

    # --- webhook / incremental sync ------------------------------------------
    def setup_watch(self, integration, webhook_url, token):
        """Register a push-notification channel; return a WatchChannel."""
        raise NotImplementedError

    def stop_watch(self, integration):
        raise NotImplementedError

    def sync_events(self, integration, window_start, window_end):
        """Return changed events since integration.sync_token (or a full window
        scan if there is no token / it expired) as a SyncResult."""
        raise NotImplementedError
