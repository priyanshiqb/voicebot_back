"""
Rebuild materialized UTC availability windows.

Recovery tool for a missed beat run, and the re-verification step after a
``tzdata`` upgrade (new timezone data can move a transition, which changes every
window that spans it). Safe to run at any time: materialization is idempotent
and always converges on the current rules.

    manage.py materialize_availability [--tenant=<uuid>] [--profile=<uuid>]
                                       [--days=90] [--purge-past] [--dry-run]
"""

from django.core.management.base import BaseCommand, CommandError

from core.models.availability_profile import AvailabilityProfile
from core.services import availability_materializer as materializer
from tenant.models.Tenant import Tenant


class Command(BaseCommand):
    help = "Regenerate materialized UTC availability windows from wall-clock rules."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Limit to one tenant id.")
        parser.add_argument("--profile", help="Limit to one availability profile id.")
        parser.add_argument(
            "--days",
            type=int,
            help="Horizon in days. Defaults to the per-tenant computed horizon.",
        )
        parser.add_argument(
            "--purge-past",
            action="store_true",
            help="Also drop windows that ended over a day ago.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be rebuilt without writing.",
        )

    def handle(self, *args, **options):
        profile_id = options.get("profile")
        tenant_id = options.get("tenant")
        days = options.get("days")
        dry_run = options.get("dry_run")

        if options.get("purge_past") and not dry_run:
            purged = materializer.purge_past_windows()
            self.stdout.write(f"purged {purged} past windows")

        if profile_id:
            profile = (
                AvailabilityProfile.all_objects.filter(pk=profile_id)
                .select_related("timezone", "tenant")
                .prefetch_related("rules")
                .first()
            )
            if profile is None:
                raise CommandError(f"No availability profile with id {profile_id}.")
            if dry_run:
                self.stdout.write(
                    f"would rebuild profile {profile.id} ({profile.rules.count()} rules)"
                )
                return
            written = materializer.materialize_profile_from_today(profile, days=days)
            self.stdout.write(self.style.SUCCESS(f"profile {profile.id}: {written} windows"))
            return

        tenants = Tenant.objects.filter(is_deleted=False)
        if tenant_id:
            tenants = tenants.filter(pk=tenant_id)
            if not tenants.exists():
                raise CommandError(f"No tenant with id {tenant_id}.")

        total_profiles = total_windows = 0
        for tenant in tenants.iterator(chunk_size=100):
            if dry_run:
                count = AvailabilityProfile.objects.filter(
                    tenant=tenant, is_deleted=False, is_active=True
                ).count()
                horizon = days or materializer.horizon_days_for(tenant)
                self.stdout.write(
                    f"would rebuild {count} profiles for tenant {tenant.id} "
                    f"over {horizon} days"
                )
                total_profiles += count
                continue
            seen, written = materializer.materialize_tenant(tenant, days=days)
            total_profiles += seen
            total_windows += written
            self.stdout.write(f"tenant {tenant.id}: {seen} profiles, {written} windows")

        verb = "would rebuild" if dry_run else "rebuilt"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {total_profiles} profiles, {total_windows} windows"
            )
        )
