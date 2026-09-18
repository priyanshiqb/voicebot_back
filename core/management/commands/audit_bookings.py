"""
Assert the scheduling module's invariants against live data.

Every invariant here is already enforced at write time — by a database
constraint, a service-layer check, or a serializer. This command exists for the
rows that got in another way: a fixture, a shell session, a data migration, a
restore from an older schema, or a bug that has since been fixed but left
residue. It is monitoring, not a fix.

    manage.py audit_bookings [--tenant=<uuid>] [--check=<name>] [--quiet] [--limit=N]

Exits 1 if anything is found, so it can be wired to a cron alert.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, F, Q
from django.utils import timezone

from core.models.availability_profile import AvailabilityProfile
from core.models.availability_window import AvailabilityWindow
from core.models.booking import HELD_STATUS, Booking
from core.models.booking_participant import BookingParticipant
from core.models.notification_log import NotificationLog
from tenant.models.Tenant import Tenant
from utils.querysets import batched_queryset, batched_values

# How long past its expiry a HELD booking may sit before it counts as stranded.
# The release task runs every 60s; this is generous enough that a slow run or a
# brief worker restart does not raise an alert.
HOLD_GRACE = timedelta(minutes=10)


class Finding:
    """One violated invariant, with enough context to act on it."""

    def __init__(self, check, severity, message, ids=()):
        self.check = check
        self.severity = severity  # "ERROR" (data is wrong) | "WARN" (probably wrong)
        self.message = message
        self.ids = list(ids)

    def __str__(self):
        head = f"[{self.severity}] {self.check}: {self.message}"
        if self.ids:
            shown = ", ".join(str(i) for i in self.ids[:5])
            more = f" (+{len(self.ids) - 5} more)" if len(self.ids) > 5 else ""
            head += f"\n         ids: {shown}{more}"
        return head


def _ids(qs, limit):
    return list(qs.values_list("id", flat=True)[:limit])


# --- checks ------------------------------------------------------------------
# Each takes (scope, limit) and returns a list of Finding. `scope` is a dict of
# filter kwargs, empty for all tenants.


# The guarantees the schema itself provides. Scanning data to re-derive what a
# present constraint already prevents finds nothing by construction; the failure
# mode that IS real is the constraint going missing — a faked migration, a
# restore from an older dump, or btree_gist not installed on a new database. So
# check the catalog, which is O(1), instead of the rows, which is O(n).
REQUIRED_CONSTRAINTS = {
    "core_booking_no_overlap_per_host": "hosts can be double-booked",
    "booking_end_after_start": "bookings can end before they start",
    "booking_effective_end_after_start": "effective windows can invert",
    "booking_held_requires_expiry": "holds can be created that never expire",
    "availability_window_end_after_start": "zero-length availability windows can be written",
    "unique_availability_window_occurrence": "materialization can duplicate windows",
    "unique_notification_dedup_key": "customers can receive duplicate emails",
}


def check_schema_guards(scope, limit):
    """Invariants 1 and 6, and the window constraints.

    These are unreachable through the ORM *while the constraints exist*. This
    asserts they exist.
    """
    from django.db import connection as db

    with db.cursor() as cursor:
        cursor.execute("SELECT conname FROM pg_constraint")
        present = {row[0] for row in cursor.fetchall()}
        cursor.execute("SELECT extname FROM pg_extension")
        extensions = {row[0] for row in cursor.fetchall()}

    findings = []
    for name, consequence in REQUIRED_CONSTRAINTS.items():
        if name not in present:
            findings.append(
                Finding(
                    "missing-constraint",
                    "ERROR",
                    f"constraint {name!r} is not present — {consequence}",
                )
            )
    if "btree_gist" not in extensions:
        findings.append(
            Finding(
                "missing-constraint",
                "ERROR",
                "the btree_gist extension is not installed — the booking overlap "
                "exclusion constraint cannot exist without it",
            )
        )
    return findings


def check_effective_times(scope, limit):
    """Invariant 2, the half the database does NOT enforce.

    Postgres checks that each window is internally ordered, but nothing checks
    that `effective_*` BRACKETS the visible times. If it does not, the exclusion
    constraint protects a narrower range than the booking actually occupies and
    buffers silently stop participating in overlap detection.
    """
    bad = Booking.objects.filter(is_deleted=False, **scope).filter(
        Q(effective_start_time__gt=F("start_time"))
        | Q(effective_end_time__lt=F("end_time"))
    )
    count = bad.count()
    if not count:
        return []
    return [
        Finding(
            "effective-times",
            "ERROR",
            f"{count} booking(s) whose effective_* window does not bracket "
            f"start_time/end_time — buffers are not protecting these slots",
            _ids(bad, limit),
        )
    ]


# Invariant 3 ("nothing is auto-assigned to a host pool") has no check here on
# purpose: `Booking.primary_host` is NOT NULL, so a hostless booking cannot exist
# in the table at all. A check for it would be permanently dead code.


def check_tenant_coherence(scope, limit):
    """Invariant 4. Services are not tenant-scoped by the framework, so a missed
    filter writes a row whose FKs point across the tenant boundary. Nothing else
    detects this — the row reads fine from either side."""
    findings = []

    mismatched = Booking.objects.filter(is_deleted=False, **scope).exclude(
        event_template__tenant_id=F("tenant_id")
    )
    if mismatched.exists():
        findings.append(
            Finding(
                "cross-tenant-template",
                "ERROR",
                f"{mismatched.count()} booking(s) whose event_template belongs to "
                f"another tenant",
                _ids(mismatched, limit),
            )
        )

    part_scope = {f"booking__{k}": v for k, v in scope.items()}
    parts = BookingParticipant.objects.filter(is_deleted=False, **part_scope).exclude(
        booking__tenant_id=F("tenant_id")
    )
    if parts.exists():
        findings.append(
            Finding(
                "cross-tenant-participant",
                "ERROR",
                f"{parts.count()} participant(s) whose tenant differs from their booking's",
                _ids(parts, limit),
            )
        )

    windows = AvailabilityWindow.objects.filter(**scope).exclude(
        profile__tenant_id=F("tenant_id")
    )
    if windows.exists():
        findings.append(
            Finding(
                "cross-tenant-window",
                "ERROR",
                f"{windows.count()} availability window(s) whose tenant differs from "
                f"their profile's",
                _ids(windows, limit),
            )
        )
    return findings


def check_participantless_bookings(scope, limit):
    """A booking with no participants notifies nobody — `_emit` short-circuits on
    an empty recipient list, so it fails completely silently."""
    bad = (
        Booking.objects.filter(is_deleted=False, **scope)
        .exclude(status=Booking.Status.CANCELED)
        .annotate(n=Count("participants", filter=Q(participants__is_deleted=False)))
        .filter(n=0)
    )
    count = bad.count()
    if not count:
        return []
    return [
        Finding(
            "no-participants",
            "WARN",
            f"{count} active booking(s) with no participants — these notify nobody",
            _ids(bad, limit),
        )
    ]


def check_stranded_holds(scope, limit):
    """A HELD booking counts as busy. If the release task stops, its slot is
    blocked indefinitely and nothing else clears it."""
    cutoff = timezone.now() - HOLD_GRACE
    bad = Booking.objects.filter(
        is_deleted=False, status=HELD_STATUS, hold_expires_at__lt=cutoff, **scope
    )
    count = bad.count()
    if not count:
        return []
    return [
        Finding(
            "stranded-hold",
            "ERROR",
            f"{count} hold(s) expired over {HOLD_GRACE} ago and still blocking their "
            f"slot — is cleanup_expired_holds running?",
            _ids(bad, limit),
        )
    ]


def check_unmaterialized_profiles(scope, limit):
    """The failure mode materialization introduced: a host with rules but no
    future windows is invisible to the engine and raises no error on its own."""
    now = timezone.now()
    materialized = set(
        AvailabilityWindow.objects.filter(end_utc__gt=now, **scope).values_list(
            "profile_id", flat=True
        )
    )
    profiles = (
        AvailabilityProfile.objects.filter(is_deleted=False, is_active=True, **scope)
        .annotate(n=Count("rules", filter=Q(rules__is_deleted=False)))
        .filter(n__gt=0)
    )
    stale = [
        p.id
        for p in batched_queryset(profiles, batch_size=500)
        if p.id not in materialized
    ]
    if not stale:
        return []
    return [
        Finding(
            "unmaterialized-profile",
            "ERROR",
            f"{len(stale)} active profile(s) have availability rules but no future "
            f"windows — these hosts produce zero slots. Run "
            f"`manage.py materialize_availability`",
            stale[:limit],
        )
    ]


def check_orphan_windows(scope, limit):
    """Windows whose profile is gone, inactive, or soft-deleted. CASCADE handles a
    hard delete, but a soft-delete or deactivation only purges through the service
    path — a direct ORM write leaves the host bookable from a disabled profile."""
    bad = AvailabilityWindow.objects.filter(**scope).filter(
        Q(profile__is_deleted=True)
        | Q(profile__is_active=False)
        | Q(rule__is_deleted=True)
    )
    count = bad.count()
    if not count:
        return []
    return [
        Finding(
            "orphan-window",
            "ERROR",
            f"{count} window(s) belong to a deleted/inactive profile or a deleted rule "
            f"— these advertise availability that should not exist",
            _ids(bad, limit),
        )
    ]


def check_window_horizon(scope, limit):
    """Windows exist but the horizon is closing. Availability narrows silently as
    it is consumed, so this warns before hosts go dark rather than after."""
    now = timezone.now()
    findings = []
    profiles = AvailabilityProfile.objects.filter(
        is_deleted=False, is_active=True, **scope
    )
    at_risk = []
    # batched_values appends the pk to each tuple, which here is the id itself.
    for profile_id, _pk in batched_values(profiles, ["id"], batch_size=500):
        last = (
            AvailabilityWindow.objects.filter(profile_id=profile_id)
            .order_by("-end_utc")
            .values_list("end_utc", flat=True)
            .first()
        )
        if last and (last - now) < timedelta(days=7):
            at_risk.append(profile_id)
    if at_risk:
        findings.append(
            Finding(
                "closing-horizon",
                "WARN",
                f"{len(at_risk)} profile(s) have under 7 days of materialized windows "
                f"left — the daily horizon task may not be running",
                at_risk[:limit],
            )
        )
    return findings


def check_stuck_notifications(scope, limit):
    """NotificationLog rows stuck PENDING.

    `_emit` leaves the row PENDING and the task writes SENT or FAILED, so a row
    still PENDING long afterwards means the task never ran or died without its
    error handler — and the dedup ledger will now suppress a retry forever.
    """
    cutoff = timezone.now() - timedelta(hours=1)
    stuck = NotificationLog.objects.filter(
        is_deleted=False,
        status=NotificationLog.Status.PENDING,
        created_at__lt=cutoff,
        **scope,
    )
    count = stuck.count()
    if not count:
        return []
    return [
        Finding(
            "stuck-notification",
            "WARN",
            f"{count} notification(s) still PENDING over an hour after being queued "
            f"— the dedup key now blocks any retry",
            _ids(stuck, limit),
        )
    ]


CHECKS = {
    "schema-guards": check_schema_guards,
    "effective-times": check_effective_times,
    "tenant-coherence": check_tenant_coherence,
    "no-participants": check_participantless_bookings,
    "stranded-hold": check_stranded_holds,
    "unmaterialized-profile": check_unmaterialized_profiles,
    "orphan-window": check_orphan_windows,
    "closing-horizon": check_window_horizon,
    "stuck-notification": check_stuck_notifications,
}


class Command(BaseCommand):
    help = "Assert scheduling invariants against live data. Exits 1 if violated."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Limit to one tenant id.")
        parser.add_argument(
            "--check",
            help=f"Run one check. One of: {', '.join(sorted(CHECKS))}",
        )
        parser.add_argument(
            "--limit", type=int, default=20, help="Max ids reported per finding."
        )
        parser.add_argument(
            "--quiet", action="store_true", help="Print findings only, no progress."
        )

    def handle(self, *args, **options):
        tenant_id = options.get("tenant")
        only = options.get("check")
        limit = options["limit"]
        quiet = options["quiet"]

        if only and only not in CHECKS:
            raise CommandError(
                f"Unknown check {only!r}. Available: {', '.join(sorted(CHECKS))}"
            )

        scope = {}
        if tenant_id:
            if not Tenant.objects.filter(pk=tenant_id).exists():
                raise CommandError(f"No tenant with id {tenant_id}.")
            scope["tenant_id"] = tenant_id

        selected = {only: CHECKS[only]} if only else CHECKS
        findings = []
        for name, fn in selected.items():
            if not quiet:
                self.stdout.write(f"  checking {name} …", ending="\r")
            findings.extend(fn(scope, limit))

        if not quiet:
            self.stdout.write(" " * 40, ending="\r")

        if not findings:
            self.stdout.write(
                self.style.SUCCESS(f"OK — {len(selected)} check(s) passed, no violations")
            )
            return

        errors = [f for f in findings if f.severity == "ERROR"]
        for finding in findings:
            style = self.style.ERROR if finding.severity == "ERROR" else self.style.WARNING
            self.stdout.write(style(str(finding)))

        summary = f"{len(findings)} finding(s): {len(errors)} error, {len(findings) - len(errors)} warning"
        # CommandError rather than a return value: `handle`'s return value is
        # written to stdout by Django, and a non-zero exit is what a cron alert
        # actually keys on.
        raise CommandError(summary)
