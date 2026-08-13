"""
Prune login sessions and SimpleJWT's refresh-token blacklist.

Thin wrapper over ``user.services.sessions.sweep`` — the same code the nightly beat
task runs, so an operator running this by hand and the scheduler cannot drift apart.
See that module for what is removed and why.
"""

from django.core.management.base import BaseCommand

from user.services import sessions as session_service


class Command(BaseCommand):
    help = (
        "Deactivates past-expiry login sessions, deletes those older than the "
        "retention window, and flushes SimpleJWT's expired blacklist rows."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--older-than-days",
            type=int,
            default=None,
            help=(
                "Delete sessions whose expiry is further back than this. Defaults "
                "to settings.USER_SESSION_RETENTION_DAYS."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without deleting anything.",
        )

    def handle(self, *args, **options):
        if options["dry_run"]:
            would_purge = session_service.purge_expired_sessions(
                older_than_days=options["older_than_days"], dry_run=True
            )
            self.stdout.write(
                self.style.NOTICE(
                    f"Would delete {would_purge} expired session row(s). "
                    f"Nothing was changed, and the blacklist was not flushed."
                )
            )
            return str(would_purge)

        deactivated = session_service.deactivate_expired_sessions()
        purged = session_service.purge_expired_sessions(
            older_than_days=options["older_than_days"]
        )
        flushed = session_service.flush_expired_blacklist_tokens()

        self.stdout.write(
            self.style.SUCCESS(
                f"Sessions: {deactivated} marked inactive, {purged} deleted."
            )
        )
        self.stdout.write(
            self.style.SUCCESS("Refresh-token blacklist flushed.")
            if flushed
            else self.style.NOTICE("token_blacklist not installed; nothing flushed.")
        )
        return str(purged)
