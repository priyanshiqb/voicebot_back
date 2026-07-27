"""Minimal, dependency-free RFC 5545 (iCalendar) builder for booking invites."""

from datetime import timezone as dt_timezone

from django.utils import timezone


def _escape(text):
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fmt(dt):
    """Format an aware datetime as a UTC iCalendar timestamp (e.g. 20260101T120000Z)."""
    return dt.astimezone(dt_timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(
    *,
    uid,
    start,
    end,
    summary,
    description="",
    location="",
    organizer_email=None,
    attendee_emails=(),
    sequence=0,
    method="REQUEST",
    status="CONFIRMED",
    dtstamp=None,
):
    """Return an iCalendar document (str) for a single VEVENT.

    ``sequence`` must increase on each update; ``method`` is REQUEST for
    new/updated invites and CANCEL for cancellations (status CANCELLED).
    """
    dtstamp = dtstamp or timezone.now()
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//VoiceBot//Scheduling//EN",
        "CALSCALE:GREGORIAN",
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_fmt(dtstamp)}",
        f"DTSTART:{_fmt(start)}",
        f"DTEND:{_fmt(end)}",
        f"SEQUENCE:{sequence}",
        f"STATUS:{status}",
        f"SUMMARY:{_escape(summary)}",
    ]
    if description:
        lines.append(f"DESCRIPTION:{_escape(description)}")
    if location:
        lines.append(f"LOCATION:{_escape(location)}")
    if organizer_email:
        lines.append(f"ORGANIZER:mailto:{organizer_email}")
    for addr in attendee_emails:
        lines.append(f"ATTENDEE;ROLE=REQ-PARTICIPANT;RSVP=TRUE:mailto:{addr}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"
