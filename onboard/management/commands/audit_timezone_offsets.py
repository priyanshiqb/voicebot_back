"""
Report (and optionally fix) drift between the stored ``Timezone.offset`` display
string and the zone's real standard-time offset from ``tzdata``.

``offset`` is a display default only — nothing in the scheduling engine reads it —
so drift is cosmetic rather than dangerous. It is still worth correcting: the
column is surfaced in API payloads and timezone pickers, and a zone showing the
wrong offset undermines trust in every other time the product displays.

Run after every ``tzdata`` upgrade, and in the dependency-bump runbook: a lockfile
refresh can move ``tzdata`` without anyone intending a timezone change.

Exits non-zero when drift is found (and not fixed) so CI or cron can gate on it.
"""

from django.core.management.base import BaseCommand, CommandError

from onboard.models.timezone import Timezone


def _label(delta):
    total = int(delta.total_seconds())
    sign = "+" if total >= 0 else "-"
    hours, minutes = divmod(abs(total) // 60, 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


class Command(BaseCommand):
    help = "Audit Timezone.offset against real standard-time offsets from tzdata."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fix",
            action="store_true",
            help="Rewrite drifted offset strings instead of only reporting them.",
        )
        parser.add_argument(
            "--year",
            type=int,
            default=None,
            help="Year to sample standard time for (default: current year).",
        )

    def handle(self, *args, **options):
        fix = options["fix"]
        year = options["year"]

        drifted, unresolvable = [], []

        for tz in Timezone.objects.all().order_by("timezone"):
            try:
                expected = _label(tz.standard_offset(year=year))
            except Exception as exc:  # noqa: BLE001 — a bad row must not abort the audit
                unresolvable.append((tz, str(exc)))
                continue
            if (tz.offset or "").strip() != expected:
                drifted.append((tz, tz.offset, expected))

        for tz, error in unresolvable:
            self.stderr.write(
                self.style.ERROR(f"  UNRESOLVABLE  {tz.timezone!r}: {error}")
            )

        if not drifted:
            self.stdout.write(
                self.style.SUCCESS("All Timezone.offset values match tzdata.")
            )
            # CommandError, not a return value: Django writes whatever handle()
            # returns to stdout, so it cannot carry an exit status.
            if unresolvable:
                raise CommandError(f"{len(unresolvable)} timezone(s) could not be resolved.")
            return

        self.stdout.write(
            self.style.WARNING(f"{len(drifted)} timezone(s) with drifted offset:")
        )
        for tz, stored, expected in drifted:
            self.stdout.write(f"  {tz.timezone:<28} {stored or '(blank)':>10} -> {expected}")

        if not fix:
            raise CommandError(
                f"{len(drifted)} timezone(s) have a drifted offset. "
                "Re-run with --fix to correct them."
            )

        for tz, _stored, expected in drifted:
            tz.offset = expected
            tz.save(update_fields=["offset", "updated_at"])
        self.stdout.write(self.style.SUCCESS(f"\nFixed {len(drifted)} timezone(s)."))
        if unresolvable:
            raise CommandError(f"{len(unresolvable)} timezone(s) could not be resolved.")
