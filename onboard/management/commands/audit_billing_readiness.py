"""Is this system ready to start refusing things?

Credits and entitlements both meter before they block, which only helps if somebody
looks at what the meters say before flipping the switch. This is that look.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Sum
from django.utils import timezone

from onboard.constants.entitlement_resource import EntitlementResource
from onboard.models import Subscription
from onboard.services import entitlements as entitlement_service
from onboard.services import subscriptions as subscription_service
from tenant.models import Tenant, TenantSettings
from wallet.models import CreditLedger, CreditRate, Wallet


class Command(BaseCommand):
    """Report every tenant's readiness for enforcement, and optionally enable it.

    Three questions, per tenant:

    1. Does their wallet balance match its ledger? (drift means a bug, not a policy
       decision — never enable enforcement on a tenant whose numbers disagree)
    2. Are they within every plan limit? (enabling caps on a tenant already over one
       breaks a working account with no warning)
    3. Can their plan price a call at all? (an unpriced tier fails at call time)

        python manage.py audit_billing_readiness
        python manage.py audit_billing_readiness --verbose
        python manage.py audit_billing_readiness --enable-safe
    """

    help = "Audit tenants for credit and entitlement enforcement readiness."

    def add_arguments(self, parser):
        parser.add_argument("--verbose", action="store_true",
                            help="Show every resource for every tenant, not just problems.")
        parser.add_argument("--enable-safe", action="store_true",
                            help="Turn on both enforcement flags for tenants with no findings.")

    def handle(self, *args, **options):
        verbose = options["verbose"]
        enable_safe = options["enable_safe"]

        self._catalogue_health()

        tenants = Tenant.objects.order_by("name")
        ready, blocked, no_plan = [], [], []

        self.stdout.write(self.style.MIGRATE_HEADING("\nPER-TENANT AUDIT"))

        for tenant in tenants:
            findings = []
            subscription = subscription_service.current_for(tenant.id)

            if subscription is None:
                no_plan.append(tenant)
                if verbose:
                    self.stdout.write(f"  {tenant.name[:28]:30} no active subscription")
                continue

            findings += self._wallet_findings(tenant)
            findings += self._entitlement_findings(tenant, subscription, verbose)

            if findings:
                blocked.append((tenant, findings))
                self.stdout.write(
                    self.style.WARNING(f"  {tenant.name[:28]:30} {subscription.plan.code}")
                )
                for finding in findings:
                    self.stdout.write(f"      - {finding}")
            else:
                ready.append(tenant)
                if verbose:
                    self.stdout.write(
                        self.style.SUCCESS(f"  {tenant.name[:28]:30} {subscription.plan.code}  ready")
                    )

        self._summary(ready, blocked, no_plan)

        if enable_safe:
            self._enable(ready)

    # ------------------------------------------------------------------ checks

    def _catalogue_health(self):
        """Can every live plan price every call tier?

        A missing rate is not a free call — it raises at consumption time. Better to
        learn that here than from a tenant whose campaign stopped.
        """
        self.stdout.write(self.style.MIGRATE_HEADING("CATALOGUE"))

        from onboard.models import Plan

        required = {op.value for op in CreditRate.Operation}
        priced = {}
        for plan_id, operation in CreditRate.objects.filter(
            is_deleted=False, effective_to__isnull=True
        ).values_list("plan_id", "operation"):
            priced.setdefault(plan_id, set()).add(operation)

        problems = 0
        for plan in Plan.objects.filter(is_active=True, is_deleted=False).order_by("sort_order"):
            missing = required - priced.get(plan.pk, set())
            entitlements = plan.entitlements.filter(is_deleted=False).count()
            issues = []
            if missing:
                issues.append(f"no rate for {', '.join(sorted(missing))}")
            if plan.is_minutes_based:
                # A minutes plan grants per minute, so a zero here means nothing.
                # What can break it is the basis row going missing or losing its
                # rupee price after activation -- a rate edit, not a plan edit, so
                # the activation gate never sees it.
                if plan.credits_per_minute() in (None, 0):
                    issues.append(
                        f"sold at {plan.credit_basis} but that tier grants no credits"
                    )
                if plan.basis_rate() is None:
                    issues.append(f"no open rate for its basis tier {plan.credit_basis}")
            else:
                issues.append("has no basis tier, so it can neither be priced nor grant credits")

            if plan.credit_basis and not plan.is_minutes_based:
                issues.append("has a basis tier but cannot be sold by the minute")
            if entitlements < len(EntitlementResource.values):
                issues.append(f"only {entitlements}/{len(EntitlementResource.values)} entitlements configured")

            if issues:
                problems += 1
                self.stdout.write(self.style.WARNING(f"  {plan.code:26} {'; '.join(issues)}"))
            else:
                summary = (
                    f"{plan.min_minutes or 'any'}-{plan.max_minutes or 'any'} min "
                    f"@ {plan.basis_rate().price_per_minute_inr}/min"
                    if plan.is_minutes_based
                    else "no basis"
                )
                self.stdout.write(
                    f"  {plan.code:26} {summary}, "
                    f"{len(priced.get(plan.pk, set()))} rates, {entitlements} entitlements"
                )

        problems += self._ladder_findings()

        if not problems:
            self.stdout.write(self.style.SUCCESS("  every active plan is fully configured"))

    def _ladder_findings(self):
        """Overlaps and gaps across each minutes ladder.

        Activation refuses a plan that would break its ladder, but a ladder can also
        be broken *after* the fact -- by retiring the plan that covered the middle of
        it, or by editing a band on a plan that is already live. Nothing else looks.
        """
        from onboard.models import Plan
        from onboard.services import plan_selection

        found = 0
        for cycle in Plan.BillingCycleChoices.values:
            ladder = plan_selection.ladder(billing_cycle=cycle)
            if not ladder:
                continue

            problems = plan_selection.ladder_problems(ladder)
            for problem in problems:
                found += 1
                self.stdout.write(
                    self.style.ERROR(f"  {cycle} ladder: {problem['message']}")
                )
            if not problems:
                self.stdout.write(
                    f"  {cycle} ladder: {len(ladder)} band(s), no overlaps or gaps"
                )
        return found

    def _wallet_findings(self, tenant):
        """Balance must agree with the ledger, per bucket."""
        wallet = Wallet.objects.filter(tenant=tenant).first()
        if wallet is None:
            return ["no wallet (created on first use, but nothing has stamped a rate card)"]

        findings = []
        total = wallet.entries.aggregate(s=Sum("amount"))["s"] or 0
        if total != wallet.balance:
            findings.append(f"LEDGER DRIFT: balance {wallet.balance} vs ledger {total}")

        for bucket, held in (
            (CreditLedger.Bucket.ALLOCATED, wallet.allocated_balance),
            (CreditLedger.Bucket.PURCHASED, wallet.purchased_balance),
        ):
            summed = wallet.entries.filter(bucket=bucket).aggregate(s=Sum("amount"))["s"] or 0
            if summed != held:
                findings.append(f"LEDGER DRIFT ({bucket}): {held} vs ledger {summed}")

        if not wallet.rate_snapshot:
            findings.append("no rate card stamped — calls would fail to price")

        return findings

    def _entitlement_findings(self, tenant, subscription, verbose):
        """Anything already over its cap."""
        findings = []
        for row in entitlement_service.summary(tenant.id, subscription, with_usage=True):
            limit = row["limit"]
            if limit is None:
                continue
            if row["used"] > limit:
                findings.append(
                    f"OVER LIMIT: {row['resource']} {row['used']}/{limit} "
                    f"({row['included']} included + {row['additional']} bought)"
                )
            elif verbose and row["used"]:
                self.stdout.write(
                    f"      {row['resource']:20} {row['used']}/{limit}"
                )
        return findings

    # ----------------------------------------------------------------- output

    def _summary(self, ready, blocked, no_plan):
        self.stdout.write(self.style.MIGRATE_HEADING("\nSUMMARY"))
        self.stdout.write(f"  ready for enforcement : {len(ready)}")
        self.stdout.write(f"  blocked by findings   : {len(blocked)}")
        self.stdout.write(f"  no active subscription: {len(no_plan)}")

        enforced_credits = TenantSettings.objects.filter(credits_enforced=True).count()
        enforced_limits = TenantSettings.objects.filter(entitlements_enforced=True).count()
        self.stdout.write(
            f"\n  currently enforcing   : {enforced_credits} credits, {enforced_limits} entitlements"
        )

        if blocked:
            self.stdout.write(self.style.WARNING(
                "\n  Resolve the findings above before enabling enforcement for those "
                "tenants. Ledger drift is a bug to investigate, not a number to "
                "correct; over-limit tenants need either an upgrade, bought capacity, "
                "or a deliberate decision to grandfather them."
            ))

    def _enable(self, ready):
        self.stdout.write(self.style.MIGRATE_HEADING("\nENABLING"))
        if not ready:
            self.stdout.write("  nothing to enable")
            return

        for tenant in ready:
            settings_row = TenantSettings.for_tenant(tenant.id)
            settings_row.credits_enforced = True
            settings_row.entitlements_enforced = True
            settings_row.save(update_fields=[
                "credits_enforced", "entitlements_enforced", "updated_at",
            ])

        self.stdout.write(self.style.SUCCESS(
            f"  enforcement enabled for {len(ready)} tenant(s)"
        ))
