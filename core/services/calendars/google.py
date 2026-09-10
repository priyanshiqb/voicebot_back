"""Google Calendar provider. All google/* SDK imports are lazy (inside methods)
so the module loads without the libraries and tests can substitute a fake."""

import hashlib
import logging
import os
from datetime import datetime
from datetime import timezone as dt_timezone

from django.conf import settings

from core.services.calendars.base import (
    BusyInterval,
    CalendarProvider,
    ChangedEvent,
    OAuthTokens,
    SyncResult,
    WatchChannel,
)

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URI = "https://oauth2.googleapis.com/revoke"


def _pkce_cache_key(state):
    """
    Cache key for a flow's PKCE verifier.

    Hashed rather than interpolated: `state` is a signed payload a few hundred
    characters long, and Django's cache key limit is 250 — memcached rejects a longer
    key outright and every backend warns. The digest is of a value we signed ourselves,
    so a collision is not something an attacker can steer.
    """
    return "pkce_" + hashlib.sha256(state.encode()).hexdigest()


def _relax_token_scope():
    """
    Stop oauthlib raising when Google hands back a different scope list than we asked
    for.

    It always does, sooner or later: `include_granted_scopes=true` means the token
    carries every scope this user has already granted the client, and Google also
    normalises the set (adding `openid`, dropping duplicates, reordering). oauthlib
    treats any difference as a `Warning` raised out of `fetch_token`, which the view
    can only report as "Failed to connect calendar" — the connect flow then fails for
    exactly the users who have connected something else before, and works in testing
    with a fresh account.

    The env var is oauthlib's own documented switch and is read per call, so setting
    it here rather than in the process environment keeps it scoped to this exchange.
    """
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"


def _parse_rfc3339(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _event_body(booking):
    body = {
        "summary": booking.title,
        "start": {"dateTime": booking.start_time.astimezone(dt_timezone.utc).isoformat()},
        "end": {"dateTime": booking.end_time.astimezone(dt_timezone.utc).isoformat()},
    }
    
    attendees = []
    for participant in booking.participants.filter(is_deleted=False):
        if participant.email:
            attendees.append({"email": participant.email, "displayName": participant.name})
            
    if attendees:
        body["attendees"] = attendees
        
    if booking.event_template.location_type == "VIDEO":
        body["conferenceData"] = {
            "createRequest": {
                "requestId": booking.id.hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"}
            }
        }
        
    return body


def _parse_event_dt(node):
    if not node:
        return None
    if node.get("dateTime"):
        return _parse_rfc3339(node["dateTime"])
    if node.get("date"):  # all-day -> treat as UTC midnight
        return datetime.fromisoformat(node["date"]).replace(tzinfo=dt_timezone.utc)
    return None


def _to_changed_event(item):
    cancelled = item.get("status") == "cancelled"
    return ChangedEvent(
        external_id=item.get("id", ""),
        cancelled=cancelled,
        start=None if cancelled else _parse_event_dt(item.get("start")),
        end=None if cancelled else _parse_event_dt(item.get("end")),
    )


class GoogleCalendarProvider(CalendarProvider):
    name = "GOOGLE"

    @classmethod
    def _flow(cls, state=None):
        from google_auth_oauthlib.flow import Flow

        client_config = {
            "web": {
                "client_id": settings.GOOGLE_CALENDAR_CLIENT_ID,
                "client_secret": settings.GOOGLE_CALENDAR_CLIENT_SECRET,
                "auth_uri": GOOGLE_AUTH_URI,
                "token_uri": GOOGLE_TOKEN_URI,
                "redirect_uris": [settings.GOOGLE_CALENDAR_REDIRECT_URI],
            }
        }
        flow = Flow.from_client_config(
            client_config, scopes=settings.GOOGLE_CALENDAR_SCOPES, state=state
        )
        flow.redirect_uri = settings.GOOGLE_CALENDAR_REDIRECT_URI
        return flow

    @classmethod
    def authorization_url(cls, state):
        from django.core.cache import cache

        flow = cls._flow(state=state)
        url, _ = flow.authorization_url(
            access_type="offline", include_granted_scopes="true", prompt="consent"
        )
        if getattr(flow, "code_verifier", None):
            cache.set(_pkce_cache_key(state), flow.code_verifier, timeout=600)
        return url

    @classmethod
    def exchange_code(cls, code, state=None):
        from django.core.cache import cache

        flow = cls._flow()
        kwargs = {"code": code}
        if state:
            cv = cache.get(_pkce_cache_key(state))
            if cv:
                kwargs["code_verifier"] = cv

        _relax_token_scope()
        flow.fetch_token(**kwargs)
        creds = flow.credentials
        return OAuthTokens(
            access_token=creds.token or "",
            refresh_token=creds.refresh_token or "",
            token_expires_at=(
                creds.expiry.replace(tzinfo=dt_timezone.utc) if creds.expiry else None
            ),
            scope=" ".join(creds.scopes or []),
            account_email=cls._account_email(creds),
        )

    @classmethod
    def _account_email(cls, creds):
        try:
            from googleapiclient.discovery import build

            service = build("calendar", "v3", credentials=creds, cache_discovery=False)
            return service.calendars().get(calendarId="primary").execute().get("id", "")
        except Exception:  # noqa: BLE001
            return ""

    def _credentials(self, integration):
        from google.oauth2.credentials import Credentials

        return Credentials(
            token=integration.get_access_token() or None,
            refresh_token=integration.get_refresh_token() or None,
            token_uri=GOOGLE_TOKEN_URI,
            client_id=settings.GOOGLE_CALENDAR_CLIENT_ID,
            client_secret=settings.GOOGLE_CALENDAR_CLIENT_SECRET,
            scopes=settings.GOOGLE_CALENDAR_SCOPES,
        )

    def _service(self, integration):
        from googleapiclient.discovery import build

        return build(
            "calendar", "v3", credentials=self._credentials(integration), cache_discovery=False
        )

    def get_busy(self, integration, window_start, window_end):
        body = {
            "timeMin": window_start.astimezone(dt_timezone.utc).isoformat(),
            "timeMax": window_end.astimezone(dt_timezone.utc).isoformat(),
            "items": [{"id": "primary"}],
        }
        resp = self._service(integration).freebusy().query(body=body).execute()
        busy = resp.get("calendars", {}).get("primary", {}).get("busy", [])
        return [
            BusyInterval(start=_parse_rfc3339(b["start"]), end=_parse_rfc3339(b["end"]))
            for b in busy
        ]

    def push_event(self, integration, booking):
        from googleapiclient.errors import HttpError

        body = _event_body(booking)
        body["id"] = booking.id.hex
        
        kwargs = {"calendarId": "primary", "body": body, "sendUpdates": "all"}
        if "conferenceData" in body:
            kwargs["conferenceDataVersion"] = 1

        try:
            created = (
                self._service(integration)
                .events()
                .insert(**kwargs)
                .execute()
            )
            return created.get("id", ""), created.get("hangoutLink", "")
        except HttpError as exc:
            if getattr(exc, "resp", None) is not None and exc.resp.status == 409:
                return booking.id.hex, ""
            raise

    def update_event(self, integration, booking):
        if not booking.external_event_id:
            return
        body = _event_body(booking)
        kwargs = {
            "calendarId": "primary",
            "eventId": booking.external_event_id,
            "body": body,
            "sendUpdates": "all"
        }
        if "conferenceData" in body:
            kwargs["conferenceDataVersion"] = 1

        updated = self._service(integration).events().update(**kwargs).execute()
        return updated.get("hangoutLink", "")

    def delete_event(self, integration, external_event_id):
        if not external_event_id:
            return
        try:
            self._service(integration).events().delete(
                calendarId="primary", eventId=external_event_id, sendUpdates="all"
            ).execute()
        except Exception as exc:  # noqa: BLE001 — 404/410 means already gone
            logger.warning("Google delete_event ignored: %s", exc)

    def refresh_access_token(self, integration):
        from google.auth.transport.requests import Request

        creds = self._credentials(integration)
        creds.refresh(Request())
        integration.set_tokens(access_token=creds.token)
        if creds.expiry:
            integration.token_expires_at = creds.expiry.replace(tzinfo=dt_timezone.utc)
        return integration

    def revoke(self, integration):
        import requests

        token = integration.get_refresh_token() or integration.get_access_token()
        if not token:
            return
        try:
            requests.post(GOOGLE_REVOKE_URI, params={"token": token}, timeout=10)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Google revoke ignored: %s", exc)

    def setup_watch(self, integration, webhook_url, token):
        import uuid

        channel_id = str(uuid.uuid4())
        body = {
            "id": channel_id,
            "type": "web_hook",
            "address": webhook_url,
            "token": token,
        }
        resp = (
            self._service(integration)
            .events()
            .watch(calendarId="primary", body=body)
            .execute()
        )
        expiration = resp.get("expiration")
        return WatchChannel(
            channel_id=channel_id,
            resource_id=resp.get("resourceId", ""),
            expiration=(
                datetime.fromtimestamp(int(expiration) / 1000, tz=dt_timezone.utc)
                if expiration
                else None
            ),
        )

    def stop_watch(self, integration):
        if not integration.watch_channel_id or not integration.watch_resource_id:
            return
        try:
            self._service(integration).channels().stop(
                body={
                    "id": integration.watch_channel_id,
                    "resourceId": integration.watch_resource_id,
                }
            ).execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Google stop_watch ignored: %s", exc)

    def sync_events(self, integration, window_start, window_end):
        from googleapiclient.errors import HttpError

        service = self._service(integration)
        changes = []
        sync_token = integration.sync_token or ""
        page_token = None
        max_pages = 50
        pages_fetched = 0
        while pages_fetched < max_pages:
            pages_fetched += 1
            params = {"calendarId": "primary", "singleEvents": True, "showDeleted": True}
            if sync_token:
                params["syncToken"] = sync_token
            else:
                params["timeMin"] = window_start.astimezone(dt_timezone.utc).isoformat()
                params["timeMax"] = window_end.astimezone(dt_timezone.utc).isoformat()
            if page_token:
                params["pageToken"] = page_token
            try:
                resp = service.events().list(**params).execute()
            except HttpError as exc:
                if getattr(exc, "resp", None) is not None and exc.resp.status == 410:
                    # Sync token expired — restart with a full window scan.
                    sync_token, page_token, changes = "", None, []
                    pages_fetched = 0
                    continue
                raise
            changes.extend(_to_changed_event(item) for item in resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                return SyncResult(
                    changes=changes,
                    sync_token=resp.get("nextSyncToken", "") or sync_token,
                )
        
        logger.error("sync_events exceeded max_pages (%d) for %s", max_pages, integration.id)
        raise RuntimeError("sync_events loop limit exceeded")
