"""Undo a round of test calls on a tenant, and put their wallet back.

A tenant gets exercised before go-live — thirty or forty calls placed to prove the
thing works — and afterwards their wallet reads as though they have been trading for a
month. This puts it back.

**Two modes, and the difference matters.**

``refund`` posts a ``USAGE_REFUND`` for every test call. The credits come back to the
buckets they were drawn from, the deductions stay on the record, and the ledger still
explains itself: charged, then refunded, with a reason. Nothing is destroyed. This is
the mode the rest of the billing system is built around — ``wallet.services.wallet``
already has ``refund()`` for exactly this — and it is the right answer for anything
that touched a real customer.

``purge`` deletes the usage rows and the call logs outright, then rebuilds the wallet's
balances from what remains. It leaves no trace that the calls happened, which is the
point when they were never real business — but it is a genuine deletion of financial
history, so it asks harder before doing it.

    python manage.py purge_test_calls
    python manage.py purge_test_calls --dry-run
"""

from datetime import datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction as db_transaction
from django.db.models import Sum
from django.utils import timezone

from core.models.agent_call_log import AgentCallLog
from tenant.models import Tenant
from wallet.models import CreditLedger, Wallet
from wallet.services import reconciliation

EntryType = CreditLedger.EntryType
Bucket = CreditLedger.Bucket

#: Entry types that took credits away. Used to rebuild `lifetime_consumed` after a
#: purge, so the figure still means "what this tenant has ever spent".
CONSUMPTION_TYPES = [EntryType.USAGE_DEDUCTION, EntryType.USAGE_ADJUSTMENT]


class Command(BaseCommand):
    help = "Interactively remove a tenant's test calls and restore their credits."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show everything that would change, then exit without writing.",
        )

    # ------------------------------------------------------------------

    def _in(self, prompt):
        try:
            return input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            raise CommandError("\nCancelled; nothing was changed.")

    def _ask(self, label, *, default=None, required=True):
        suffix = f" [{default}]" if default is not None else ""
        while True:
            raw = self._in(f"  {label}{suffix}: ")
            if not raw and default is not None:
                return str(default)
            if raw or not required:
                return raw
            self.stderr.write("    Required.")

    def _ask_choice(self, label, choices, *, default_index=1):
        self.stdout.write(f"  {label}")
        for i, (_value, display) in enumerate(choices, start=1):
            marker = " (default)" if i == default_index else ""
            self.stdout.write(f"    {i}) {display}{marker}")
        while True:
            raw = self._in("    Select: ")
            if not raw:
                return choices[default_index - 1][0]
            if raw.isdigit() and 1 <= int(raw) <= len(choices):
                return choices[int(raw) - 1][0]
            self.stderr.write("    Not a listed option.")

    def _heading(self, text):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(text))
        self.stdout.write("-" * len(text))

    # ------------------------------------------------------------------

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        self.stdout.write(self.style.MIGRATE_HEADING("\nTest call cleanup"))
        self.stdout.write(
            "Removes a tenant's test calls and returns the credits they consumed.\n"
        )

        tenant = self._select_tenant()
        wallet = Wallet.objects.filter(tenant=tenant).first()
        if wallet is None:
            raise CommandError(f"{tenant.name} has no wallet; nothing to correct.")

        self._show_wallet(tenant, wallet)

        calls = self._select_calls(tenant)
        if not calls:
            self.stdout.write(self.style.WARNING("\nNo calls matched. Nothing to do."))
            return

        charges = self._charges_for(calls)
        self._show_plan(calls, charges, wallet)

        if dry_run:
            self.stdout.write(self.style.WARNING("\nDry run — nothing was changed."))
            return

        mode = self._ask_choice(
            "How should the credits be returned?",
            [
                ("refund", "Refund — post USAGE_REFUND entries, keep the history (safer)"),
                ("purge", "Purge — delete the usage rows and call logs entirely"),
            ],
            default_index=1,
        )

        if not self._confirm(tenant, calls, mode):
            self.stdout.write("Cancelled; nothing was changed.")
            return

        reason = self._ask("Reason for the record", default="Pre-launch test calls")

        if mode == "refund":
            self._do_refund(tenant, wallet, charges, reason)
        else:
            self._do_purge(tenant, wallet, calls, charges)

        self._verify(tenant, wallet)

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _select_tenant(self):
        self._heading("1. Tenant")
        raw = self._ask("Tenant subdomain, name or id")

        tenant = (
            Tenant.objects.filter(subdomain__iexact=raw).first()
            or Tenant.objects.filter(name__iexact=raw).first()
        )
        if tenant is None:
            try:
                tenant = Tenant.objects.filter(pk=raw).first()
            except Exception:
                # Not a UUID. Fall through to the name search below rather than
                # failing — an operator typing a partial name is the common case.
                tenant = None
        if tenant is None:
            matches = list(Tenant.objects.filter(name__icontains=raw)[:10])
            if not matches:
                raise CommandError(f"No tenant matches '{raw}'.")
            tenant = self._ask_choice(
                "Which tenant?",
                [(t, f"{t.name} ({t.subdomain})") for t in matches],
            )

        self.stdout.write(
            self.style.SUCCESS(f"  → {tenant.name} ({tenant.subdomain}) {tenant.id}")
        )
        return tenant

    def _show_wallet(self, tenant, wallet):
        self._heading("2. Wallet as it stands")
        w = self.stdout.write
        w(f"  Balance            {wallet.balance}")
        w(f"    allocated        {wallet.allocated_balance}")
        w(f"    purchased        {wallet.purchased_balance}")
        w(f"  Lifetime granted   {wallet.lifetime_granted}")
        w(f"  Lifetime consumed  {wallet.lifetime_consumed}")
        w(f"  Ledger entries     {CreditLedger.all_objects.filter(wallet=wallet).count()}")

        granted = (
            CreditLedger.all_objects.filter(
                wallet=wallet, entry_type=EntryType.CYCLE_ALLOCATION
            ).aggregate(total=Sum("amount"))["total"]
            or 0
        )
        if granted:
            w(f"  Cycle allocation   {granted}   ← what a fresh wallet should read")

    def _select_calls(self, tenant):
        self._heading("3. Which calls")

        total = AgentCallLog.objects.filter(tenant=tenant).count()
        self.stdout.write(f"  This tenant has {total} call log(s).\n")

        scope = self._ask_choice(
            "Scope",
            [
                ("all", "Every call this tenant has"),
                ("days", "Calls from the last N days"),
                ("range", "Calls between two dates"),
            ],
        )

        calls = AgentCallLog.objects.filter(tenant=tenant)
        if scope == "days":
            days = int(self._ask("How many days back", default="7"))
            calls = calls.filter(created_at__gte=timezone.now() - timedelta(days=days))
        elif scope == "range":
            start = self._parse_date(self._ask("From (YYYY-MM-DD)"), end=False)
            end = self._parse_date(self._ask("To (YYYY-MM-DD)"), end=True)
            calls = calls.filter(created_at__gte=start, created_at__lte=end)

        return list(calls.select_related("agent").order_by("created_at"))

    def _parse_date(self, raw, *, end):
        try:
            day = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            raise CommandError(f"'{raw}' is not a date. Use YYYY-MM-DD.")
        moment = datetime.combine(day, datetime.max.time() if end else datetime.min.time())
        return timezone.make_aware(moment)

    # ------------------------------------------------------------------
    # What it will cost
    # ------------------------------------------------------------------

    def _charges_for(self, calls):
        """Every usage row belonging to these calls, keyed by provider_call_id.

        Matched on `reference_id`, which is what `consume()` writes and what makes a
        charge traceable to its call. A charge that spanned both buckets wrote two
        rows, and the second carries a `#purchased` suffix — both are collected, or a
        refund would return only half the credits.
        """
        references = [c.provider_call_id for c in calls]
        suffixed = [f"{r}#{Bucket.PURCHASED}" for r in references]

        rows = CreditLedger.all_objects.filter(
            entry_type=EntryType.USAGE_DEDUCTION,
            reference_id__in=references + suffixed,
        )

        charges = {}
        for row in rows:
            base = row.reference_id.split("#")[0]
            charges.setdefault(base, []).append(row)
        return charges

    def _show_plan(self, calls, charges, wallet):
        self._heading("4. What will change")
        w = self.stdout.write

        w(f"  {'Started':<20} {'Duration':>9}  {'Tier':<28} {'Charged':>8}")
        w("  " + "-" * 70)

        total_credits = 0
        uncharged = 0
        for call in calls:
            rows = charges.get(call.provider_call_id, [])
            credits = sum(abs(r.amount) for r in rows)
            total_credits += credits
            if not rows:
                uncharged += 1
            started = (
                call.started_at.strftime("%Y-%m-%d %H:%M")
                if call.started_at
                else call.created_at.strftime("%Y-%m-%d %H:%M") + "*"
            )
            w(
                f"  {started:<20} {str(call.duration_seconds) + 's':>9}  "
                f"{(call.service_tier or '—'):<28} "
                f"{(str(credits) if rows else '—'):>8}"
            )

        w("")
        w(f"  Calls to remove        {len(calls)}")
        w(f"  Usage entries          {sum(len(v) for v in charges.values())}")
        if uncharged:
            w(f"  Never charged          {uncharged}  (no credits to return)")
        w(f"  Credits to return      {total_credits}")
        w("")
        w(f"  Balance now            {wallet.balance}")
        w(f"  Balance after          {wallet.balance + total_credits}")

        if any(c.started_at is None for c in calls):
            w("")
            w("  * no started_at; showing created_at instead")

        self._warn_if_not_latest(wallet, charges)

    def _warn_if_not_latest(self, wallet, charges):
        """Flag a purge that would leave stale `balance_after` on later rows.

        Each ledger row records the balance as it stood after that movement. Deleting a
        row in the middle does not rewrite the ones after it, so their `balance_after`
        becomes a number that never existed. Refunding has no such problem — it only
        ever appends.
        """
        ids = [r.pk for rows in charges.values() for r in rows]
        if not ids:
            return

        oldest = CreditLedger.all_objects.filter(pk__in=ids).order_by("created_at").first()
        newer = CreditLedger.all_objects.filter(
            wallet=wallet, created_at__gt=oldest.created_at
        ).exclude(pk__in=ids).count()

        if newer:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                f"  Note: {newer} ledger entr(ies) were written after the oldest test "
                "charge.\n"
                "  A purge leaves their `balance_after` showing a balance that will no "
                "longer\n"
                "  be reachable from the ledger. Refund does not have this problem."
            ))

    def _confirm(self, tenant, calls, mode):
        self.stdout.write("")
        if mode == "purge":
            self.stdout.write(self.style.ERROR(
                f"  PURGE deletes {len(calls)} call log(s) and their usage entries "
                "permanently.\n"
                "  Call history is append-only by design; this is the exception, not a "
                "tool to reach for twice."
            ))
        typed = self._in(f"  Type the tenant subdomain ({tenant.subdomain}) to confirm: ")
        return typed == tenant.subdomain

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _do_refund(self, tenant, wallet, charges, reason):
        from wallet.services import wallet as svc

        self._heading("5. Refunding")
        refunded = skipped = 0

        for reference in charges:
            entry = svc.refund(
                tenant.id,
                reference_id=reference,
                notes=reason,
                metadata={"test_call_cleanup": True},
            )
            if entry is None:
                skipped += 1
            else:
                refunded += 1

        self.stdout.write(f"  Refunded {refunded} charge(s).")
        if skipped:
            self.stdout.write(f"  {skipped} skipped — already refunded.")
        self.stdout.write(
            "  The call logs were left in place: a refunded charge still refers to a "
            "call\n  that happened. Re-run in purge mode if they should go too."
        )

    @db_transaction.atomic
    def _do_purge(self, tenant, wallet, calls, charges):
        self._heading("5. Purging")

        entry_ids = [r.pk for rows in charges.values() for r in rows]
        deleted_entries = CreditLedger.all_objects.filter(pk__in=entry_ids).delete()[0]
        deleted_calls = AgentCallLog.objects.filter(
            pk__in=[c.pk for c in calls]
        ).delete()[0]

        # Rebuilt from what is left, not adjusted by arithmetic. The ledger is the
        # source of truth for the balance, so deriving it is the only way to be certain
        # the two agree afterwards — and it is exactly what reconciliation checks.
        locked = Wallet.objects.select_for_update().get(pk=wallet.pk)
        remaining = CreditLedger.all_objects.filter(wallet=locked)

        sums = {
            row["bucket"]: row["total"] or 0
            for row in remaining.values("bucket").annotate(total=Sum("amount"))
        }
        locked.allocated_balance = max(0, sums.get(Bucket.ALLOCATED, 0))
        locked.purchased_balance = max(0, sums.get(Bucket.PURCHASED, 0))
        locked.balance = locked.allocated_balance + locked.purchased_balance
        locked.lifetime_consumed = abs(
            remaining.filter(entry_type__in=CONSUMPTION_TYPES).aggregate(
                total=Sum("amount")
            )["total"]
            or 0
        )
        locked.save(update_fields=[
            "allocated_balance", "purchased_balance", "balance",
            "lifetime_consumed", "updated_at",
        ])

        self.stdout.write(f"  Deleted {deleted_calls} call log(s).")
        self.stdout.write(f"  Deleted {deleted_entries} ledger entr(ies).")
        self.stdout.write(f"  Balance rebuilt from the remaining ledger: {locked.balance}")

    # ------------------------------------------------------------------

    def _verify(self, tenant, wallet):
        self._heading("6. Verification")
        wallet.refresh_from_db()

        problems = reconciliation.check(wallet)
        if problems:
            for problem in problems:
                self.stdout.write(self.style.ERROR(f"  {problem['message']}"))
            self.stdout.write(self.style.ERROR(
                "  The wallet does not reconcile. Investigate before doing anything "
                "else."
            ))
            return

        w = self.stdout.write
        w(self.style.SUCCESS("  Wallet reconciles against its ledger."))
        w(f"  Balance            {wallet.balance}")
        w(f"    allocated        {wallet.allocated_balance}")
        w(f"    purchased        {wallet.purchased_balance}")
        w(f"  Lifetime consumed  {wallet.lifetime_consumed}")
        w(f"  Remaining calls    {AgentCallLog.objects.filter(tenant=tenant).count()}")
