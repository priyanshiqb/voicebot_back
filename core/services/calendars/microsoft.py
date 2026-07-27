"""Microsoft 365 / Outlook calendar provider (Microsoft Graph).

All `msal`/`requests` imports are lazy (inside methods) so this module loads —
and tests run — without the libraries installed or Graph reachable, exactly like
the Google provider. Our DB stays the source of truth: this only reads busy times
and mirrors bookings; inbound sync only ever feeds the ExternalBusyBlock cache.
"""

import logging
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.utils import timezone

from core.services.calendars.base import (
    BusyInterval,
    CalendarProvider,
    ChangedEvent,
    OAuthTokens,
    SyncResult,
    WatchChannel,
)

logger = logging.getLogger(__name__)

# getSchedule statuses that count as "busy" (everything except free/unknown).
_BUSY_STATUSES = {"busy", "tentative", "oof", "workingElsewhere"}

# Max subscription lifetime for /me/events is ~4230 min; stay safely under it.
_SUBSCRIPTION_MINUTES = 4200

# Bounded retry on Graph throttling / transient errors.
_RETRY_STATUSES = {429, 503}
_MAX_RETRIES = 2
_MAX_RETRY_SLEEP = 5


def _graph_scopes():
    """Resource scopes for MSAL — the reserved OIDC scopes (openid/profile/
    offline_access/email) are added by MSAL automatically and must not be passed."""
    return [s for s in settings.MICROSOFT_CALENDAR_SCOPES if s.startswith("https://")]


def _parse_graph_dt(node):
    """Parse a Graph {dateTime, timeZone} node to a UTC-aware datetime.

    We always request UTC (Prefer: outlook.timezone / getSchedule defaults), so
    an offset-less value is treated as UTC. Graph emits up to 7 fractional digits,
    which we truncate to 6 for fromisoformat."""
    if not node:
        return None
    value = node.get("dateTime")
    if not value:
        return None
    value = value.rstrip("Z")
    if "." in value:
        head, frac = value.split(".", 1)
        value = f"{head}.{frac[:6]}"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed.astimezone(dt_timezone.utc)


def _event_body(booking):
    return {
        "subject": booking.title,
        "start": {
            "dateTime": booking.start_time.astimezone(dt_timezone.utc).isoformat(),
            "timeZone": "UTC",
        },
        "end": {
            "dateTime": booking.end_time.astimezone(dt_timezone.utc).isoformat(),
            "timeZone": "UTC",
        },
    }


def _to_changed_event(item):
    cancelled = ("@removed" in item) or bool(item.get("isCancelled"))
    return ChangedEvent(
        external_id=item.get("id", ""),
        cancelled=cancelled,
        start=None if cancelled else _parse_graph_dt(item.get("start")),
        end=None if cancelled else _parse_graph_dt(item.get("end")),
    )


class MicrosoftCalendarProvider(CalendarProvider):
    name = "OUTLOOK"

    # --- OAuth (MSAL) --------------------------------------------------------
    @classmethod
    def _msal_app(cls):
        import msal

        return msal.ConfidentialClientApplication(
            client_id=settings.MICROSOFT_CALENDAR_CLIENT_ID,
            client_credential=settings.MICROSOFT_CALENDAR_CLIENT_SECRET,
            authority=settings.MICROSOFT_CALENDAR_AUTHORITY,
        )

    @classmethod
    def authorization_url(cls, state):
        return cls._msal_app().get_authorization_request_url(
            _graph_scopes(),
            state=state,
            redirect_uri=settings.MICROSOFT_CALENDAR_REDIRECT_URI,
        )

    @classmethod
    def exchange_code(cls, code):
        result = cls._msal_app().acquire_token_by_authorization_code(
            code,
            scopes=_graph_scopes(),
            redirect_uri=settings.MICROSOFT_CALENDAR_REDIRECT_URI,
        )
        if "access_token" not in result:
            raise ValueError(
                result.get("error_description") or result.get("error") or "MS auth failed"
            )
        claims = result.get("id_token_claims") or {}
        email = claims.get("preferred_username") or claims.get("email") or ""
        if not email:
            email = cls._me_email(result["access_token"])
        return OAuthTokens(
            access_token=result.get("access_token", ""),
            refresh_token=result.get("refresh_token", ""),
            token_expires_at=cls._expires_at(result),
            scope=result.get("scope", ""),
            account_email=email,
        )

    @staticmethod
    def _expires_at(result):
        expires_in = result.get("expires_in")
        if not expires_in:
            return None
        return timezone.now() + timedelta(seconds=int(expires_in))

    @classmethod
    def _me_email(cls, access_token):
        try:
            import requests

            resp = requests.get(
                f"{settings.MICROSOFT_GRAPH_BASE_URL}/me",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("mail") or data.get("userPrincipalName") or ""
        except Exception:  # noqa: BLE001
            return ""

    # --- Graph REST helpers --------------------------------------------------
    def _headers(self, integration, extra=None):
        headers = {
            "Authorization": f"Bearer {integration.get_access_token()}",
            "Content-Type": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    def _request(self, method, url, integration, *, ok_statuses=None, headers=None, **kwargs):
        """Graph call with a bounded retry on throttling/transient errors."""
        import time

        import requests

        attempt = 0
        while True:
            resp = requests.request(
                method,
                url,
                headers=self._headers(integration, headers),
                timeout=20,
                **kwargs,
            )
            if resp.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                retry_after = resp.headers.get("Retry-After")
                delay = min(int(retry_after), _MAX_RETRY_SLEEP) if (
                    retry_after and retry_after.isdigit()
                ) else 1
                time.sleep(delay)
                attempt += 1
                continue
            if ok_statuses and resp.status_code in ok_statuses:
                return resp
            resp.raise_for_status()
            return resp

    def _base(self):
        return settings.MICROSOFT_GRAPH_BASE_URL

    # --- per-integration operations ------------------------------------------
    def get_busy(self, integration, window_start, window_end):
        body = {
            "schedules": [integration.account_email],
            "startTime": {
                "dateTime": window_start.astimezone(dt_timezone.utc).isoformat(),
                "timeZone": "UTC",
            },
            "endTime": {
                "dateTime": window_end.astimezone(dt_timezone.utc).isoformat(),
                "timeZone": "UTC",
            },
            "availabilityViewInterval": 15,
        }
        resp = self._request(
            "POST", f"{self._base()}/me/calendar/getSchedule", integration, json=body
        )
        schedules = resp.json().get("value", [])
        if not schedules:
            return []
        intervals = []
        for item in schedules[0].get("scheduleItems", []):
            if item.get("status") not in _BUSY_STATUSES:
                continue
            start = _parse_graph_dt(item.get("start"))
            end = _parse_graph_dt(item.get("end"))
            if start and end:
                intervals.append(BusyInterval(start=start, end=end))
        return intervals

    def push_event(self, integration, booking):
        resp = self._request(
            "POST", f"{self._base()}/me/events", integration, json=_event_body(booking)
        )
        return resp.json().get("id", "")

    def update_event(self, integration, booking):
        if not booking.external_event_id:
            return
        self._request(
            "PATCH",
            f"{self._base()}/me/events/{booking.external_event_id}",
            integration,
            json=_event_body(booking),
        )

    def delete_event(self, integration, external_event_id):
        if not external_event_id:
            return
        try:
            self._request(
                "DELETE",
                f"{self._base()}/me/events/{external_event_id}",
                integration,
                ok_statuses={404, 410},  # already gone — treat as success
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Microsoft delete_event ignored: %s", exc)

    def refresh_access_token(self, integration):
        result = self._msal_app().acquire_token_by_refresh_token(
            integration.get_refresh_token(), scopes=_graph_scopes()
        )
        if "access_token" not in result:
            raise ValueError(
                result.get("error_description") or result.get("error") or "MS refresh failed"
            )
        integration.set_tokens(access_token=result["access_token"])
        if result.get("refresh_token"):
            integration.set_tokens(refresh_token=result["refresh_token"])
        expires_at = self._expires_at(result)
        if expires_at:
            integration.token_expires_at = expires_at
        return integration

    def revoke(self, integration):
        # Microsoft Graph has no per-token revoke endpoint. Disconnect is handled
        # locally (tokens dropped, watch stopped, integration soft-deleted).
        return

    # --- webhook / incremental sync ------------------------------------------
    def setup_watch(self, integration, webhook_url, token):
        expiration = timezone.now() + timedelta(minutes=_SUBSCRIPTION_MINUTES)
        body = {
            "changeType": "created,updated,deleted",
            "notificationUrl": webhook_url,
            "resource": "/me/events",
            "expirationDateTime": expiration.astimezone(dt_timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "clientState": token,
        }
        # Graph validates notificationUrl synchronously here (validationToken echo).
        resp = self._request("POST", f"{self._base()}/subscriptions", integration, json=body)
        sub = resp.json()
        return WatchChannel(
            channel_id=sub.get("id", ""),
            resource_id=sub.get("resource", "/me/events"),
            expiration=_parse_graph_dt({"dateTime": sub.get("expirationDateTime")})
            if sub.get("expirationDateTime")
            else expiration,
        )

    def stop_watch(self, integration):
        if not integration.watch_channel_id:
            return
        try:
            self._request(
                "DELETE",
                f"{self._base()}/subscriptions/{integration.watch_channel_id}",
                integration,
                ok_statuses={404, 410},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Microsoft stop_watch ignored: %s", exc)

    def sync_events(self, integration, window_start, window_end):
        import requests

        changes = []
        delta_link = integration.sync_token or ""
        if delta_link:
            url, params = delta_link, None
        else:
            url = f"{self._base()}/me/calendarView/delta"
            params = {
                "startDateTime": window_start.astimezone(dt_timezone.utc).isoformat(),
                "endDateTime": window_end.astimezone(dt_timezone.utc).isoformat(),
            }
        prefer = {"Prefer": 'outlook.timezone="UTC", odata.maxpagesize=100'}
        while True:
            try:
                resp = self._request(
                    "GET", url, integration, headers=prefer, params=params
                )
            except requests.HTTPError as exc:
                status = getattr(exc.response, "status_code", None)
                if status in (410, 400) and delta_link:
                    # Delta token expired/invalid — restart with a full window scan.
                    delta_link = ""
                    url = f"{self._base()}/me/calendarView/delta"
                    params = {
                        "startDateTime": window_start.astimezone(dt_timezone.utc).isoformat(),
                        "endDateTime": window_end.astimezone(dt_timezone.utc).isoformat(),
                    }
                    changes = []
                    continue
                raise
            data = resp.json()
            changes.extend(_to_changed_event(item) for item in data.get("value", []))
            next_link = data.get("@odata.nextLink")
            if next_link:
                url, params = next_link, None
                continue
            return SyncResult(
                changes=changes,
                sync_token=data.get("@odata.deltaLink", "") or delta_link,
            )
