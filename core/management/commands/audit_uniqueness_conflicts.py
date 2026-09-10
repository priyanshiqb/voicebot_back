"""
Find and resolve the contacts that block the new per-tenant name uniqueness rule.

    python manage.py audit_uniqueness_conflicts                 # report only
    python manage.py audit_uniqueness_conflicts --tenant-id <uuid>
    python manage.py audit_uniqueness_conflicts --fix           # rewrites data, opt-in

Contact names became unique per tenant with BUG-001234
(``unique_active_contact_name_tenant``). The rule is retroactive: duplicates were
allowed until now, so any tenant that has been using contacts almost certainly holds
some. Migration ``core.0058`` refuses to run while any remain, because Postgres cannot
build the index and the failure it raises names one offending value with no indication
of how many more there are or what to do about them. Run this first.

**``--fix`` changes data, which is why it is opt-in.** The rule cannot be satisfied
without choosing something on the operator's behalf, so it makes the least destructive
choice available and a reversible one: duplicates are **renamed**, not deleted. The
oldest row keeps the name and each later one gains a " (2)", " (3)" suffix on its last
name. Nobody's contact disappears; the copies just become distinguishable, which is
what a human resolving this by hand would do anyway.

Report first, read the output, then decide.
"""

from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError

from core.models.contact import Contact


def _name_key(contact):
    return (
        (contact.first_name or "").strip().lower(),
        (contact.last_name or "").strip().lower(),
    )


def duplicate_contact_names(tenant_id=None):
    """
    {(tenant_id, first, last): [contact, ...]} for every name held more than once.

    Grouped in Python rather than by a ``values().annotate(Count)`` query because the
    rule is case- and whitespace-insensitive, and comparing that way in SQL needs the
    expression index that does not exist yet — the migration this unblocks is what
    creates it.
    """
    queryset = Contact.objects.all().only(
        "id", "tenant_id", "first_name", "last_name", "created_at"
    )
    if tenant_id:
        queryset = queryset.filter(tenant_id=tenant_id)

    groups = defaultdict(list)
    for contact in queryset.order_by("created_at", "id"):
        first, last = _name_key(contact)
        if not first and not last:
            continue  # unnamed contacts are exempt, as they are in the constraint
        groups[(contact.tenant_id, first, last)].append(contact)

    return {key: rows for key, rows in groups.items() if len(rows) > 1}


class Command(BaseCommand):
    help = (
        "Reports (or with --fix, resolves) the duplicate contact names that the new "
        "per-tenant uniqueness constraint rejects."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            type=str,
            default=None,
            help="Restrict to one tenant. Default: every tenant.",
        )
        parser.add_argument(
            "--fix",
            action="store_true",
            help=(
                "Resolve the conflicts by renaming the duplicate contacts. "
                "Rewrites data - read the report first."
            ),
        )

    def handle(self, *args, **options):
        tenant_id = options["tenant_id"]
        fix = options["fix"]

        groups = duplicate_contact_names(tenant_id)
        if not groups:
            self.stdout.write(
                self.style.SUCCESS("No duplicate contact names. Safe to migrate.")
            )
            return

        renamed = self._handle_contacts(groups, fix)

        if fix:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Renamed {renamed} contact(s). Re-run without --fix to confirm "
                    "nothing is left."
                )
            )
            return

        raise CommandError(
            "Conflicts found. Resolve them by hand, or re-run with --fix to apply "
            "the renames described above. The 0058 migration will fail until this "
            "reports clean."
        )

    # ------------------------------------------------------------------

    def _handle_contacts(self, groups, fix):
        if not groups:
            return 0

        total = sum(len(rows) - 1 for rows in groups.values())
        self.stdout.write(
            self.style.WARNING(
                f"{len(groups)} duplicated contact name(s), {total} row(s) to rename:"
            )
        )

        renamed = 0
        for key in sorted(groups):
            tenant_id, first, last = key
            rows = groups[key]
            display = f"{first} {last}".strip()
            self.stdout.write(f"  tenant={tenant_id} '{display}' x{len(rows)}")

            # rows are ordered oldest-first, so rows[0] keeps the name unchanged.
            for index, contact in enumerate(rows[1:], start=2):
                new_last = f"{contact.last_name} ({index})".strip()[:120]
                suffix = "" if fix else "  (dry run)"
                self.stdout.write(
                    f"    {contact.id}: last_name -> '{new_last}'{suffix}"
                )
                if fix:
                    contact.last_name = new_last
                    # update_fields, so Contact.save() does not re-derive phone_e164
                    # and reject a row whose stored number no longer parses.
                    contact.save(update_fields=["last_name", "updated_at"])
                    renamed += 1

        return renamed
