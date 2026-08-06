"""Provider-agnostic calendar interface. Concrete providers (e.g. Google) keep
their SDK imports lazy so this package loads — and tests run — without the
provider libraries installed or reachable."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


def http_status_of(exc):
    """Best-effort HTTP status from a provider exception, or None.

    Google's ``HttpError`` carries ``exc.resp.status``; other clients tend to use
    ``status_code``. Kept here rather than in the task so a second provider only
    has to be recognised in one place.
    """
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


# Retrying these is pointless: the credentials or the remote object will not come
# back on their own. Retrying them anyway is how a task burns its budget and then
# gives up silently, leaving the integration permanently broken with no signal.
AUTH_FAILURE_STATUSES = frozenset({401, 403})
GONE_STATUSES = frozenset({404, 410})
# 429 is a client error but IS worth retrying — it is rate limiting, not a defect.
RETRYABLE_CLIENT_STATUSES = frozenset({408, 429})


def is_permanent(exc):
    """True when retrying `exc` cannot succeed."""
    status = http_status_of(exc)
    if status is None:
        return False  # unknown shape: assume transient, let retries decide
    if status in RETRYABLE_CLIENT_STATUSES:
        return False
    return 400 <= status < 500


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
    def exchange_code(cls, code, state=None):
        """Exchange an auth code for OAuthTokens."""
        raise NotImplementedError

    # --- per-integration operations ------------------------------------------
    def get_busy(self, integration, window_start, window_end):
        """Return the account's busy intervals in the window (list[BusyInterval])."""
        raise NotImplementedError

    def push_event(self, integration, booking):
        """Create the mirrored event; return a tuple (external_id, meeting_url)."""
        raise NotImplementedError

    def update_event(self, integration, booking):
        """Update the mirrored event; return the meeting_url (if any)."""
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
