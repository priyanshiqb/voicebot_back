"""
Retire device tokens that stopped checking in, then delete the long-dead ones.

Thin wrapper over ``core.services.device_tokens.sweep`` — the same code the nightly
beat task runs, so an operator running this by hand and the scheduler cannot drift
apart. See that module for what "stale" means and why.

``--verify`` adds a validate-only send per stale token: FCM resolves the token and
reports what it would have done, delivering nothing. That turns "we aged this out"
into "FCM says this is gone", at the cost of one API call per token.
"""

from django.core.management.base import BaseCommand

from core.services import device_tokens as token_service


class Command(BaseCommand):
    help = (
        "Deactivates FCM device tokens not re-registered within the staleness "
        "window, and deletes dead rows older than the retention window."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--older-than-days",
            type=int,
            default=None,
            help=(
                "Delete dead rows whose last contact is further back than this. "
                "Defaults to settings.DEVICE_TOKEN_RETENTION_DAYS."
            ),
        )
        parser.add_argument(
            "--verify",
            action="store_true",
            help=(
                "Ask FCM to validate each stale token before acting (validate-only "
                "send; nothing is delivered). Costs one API call per token."
            ),
        )
        parser.add_argument(
            "--verify-limit",
            type=int,
            default=500,
            help="Cap how many tokens --verify probes in one run. Default 500.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without changing anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        if options["older_than_days"] is not None and not dry_run:
            # sweep() reads the setting; an explicit override only affects the purge.
            token_service.deactivate_stale()
            purged = token_service.purge(older_than_days=options["older_than_days"])
            self.stdout.write(self.style.SUCCESS(f"Device tokens: {purged} deleted."))
            return str(purged)

        result = token_service.sweep(
            dry_run=dry_run,
            verify=options["verify"],
            verify_limit=options["verify_limit"],
        )

        if options["verify"]:
            probed = result["probed"]
            self.stdout.write(
                self.style.NOTICE(
                    f"Probed: {probed['alive']} alive, {probed['dead']} rejected by "
                    f"FCM, {probed['unknown']} inconclusive."
                )
            )

        style = self.style.NOTICE if dry_run else self.style.SUCCESS
        verb = "Would deactivate" if dry_run else "Deactivated"
        deleted = "would delete" if dry_run else "deleted"
        if result["orphaned"]:
            retired = "Would retire" if dry_run else "Retired"
            self.stdout.write(
                style(f"{retired} {result['orphaned']} token(s) behind dead sessions.")
            )
        self.stdout.write(
            style(
                f"{verb} {result['deactivated']} stale token(s) "
                f"(no contact in {result['stale_days']} days); "
                f"{deleted} {result['purged']} dead row(s) "
                f"(retention {result['retention_days']} days)."
            )
        )
        return str(result["purged"])
