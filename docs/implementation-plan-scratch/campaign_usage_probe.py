# -*- coding: utf-8 -*-
"""Show what the campaign allowance actually reports, in every state.

Everything happens inside a transaction that is rolled back, so the dev database
is untouched.
"""
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
django.setup()

import json  # noqa: E402

from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from core.models.campaign import Campaign  # noqa: E402
from onboard.constants.entitlement_resource import EntitlementResource as R  # noqa: E402
from onboard.constants.reset_period import ResetPeriod, window_start  # noqa: E402
from onboard.services import entitlements as ent  # noqa: E402
from onboard.services import subscriptions as subs  # noqa: E402
from onboard.tests.billing_factories import (  # noqa: E402
    make_agent,
    make_campaign,
    make_plan,
    make_tenant,
)


class Rollback(Exception):
    pass


def usage(tenant, subscription):
    return ent.current_usage(tenant.id, R.CAMPAIGN_MONTHLY, subscription=subscription)


try:
    with transaction.atomic():
        ctx = make_tenant("probe")
        tenant = ctx.tenant
        plan = make_plan("probe-plan")

        # ---- 1. What commercial sets ------------------------------------ #
        row = plan.entitlements.get(resource=R.CAMPAIGN_MONTHLY)
        row.included_quantity = 5
        row.reset_period = ResetPeriod.MONTHLY
        row.is_addon_available = False
        row.save()

        print("=" * 74)
        print("1. THE PLAN ROW  (onboard_plan_entitlement)")
        print("=" * 74)
        for f in ("resource", "included_quantity", "reset_period",
                  "is_addon_available", "price_per_additional", "addon_step",
                  "max_additional"):
            print(f"   {f:<22} {getattr(row, f)!r}")

        subscription = subs.activate(tenant, plan)
        subscription.generate_plan_snapshot()
        subscription.save(update_fields=["plan_snapshot"])

        print()
        print("=" * 74)
        print("2. FROZEN ONTO THE SUBSCRIPTION  (plan_snapshot)")
        print("=" * 74)
        snap = subscription.plan_snapshot.get("entitlements", {})
        print("  ", json.dumps(snap.get(R.CAMPAIGN_MONTHLY), indent=2))
        print(f"   window anchor (subscription.created_at) = {subscription.created_at}")
        print("   current window opened at                 =",
              window_start(ResetPeriod.MONTHLY, subscription.created_at,
                           timezone.now()))

        # ---- 2. Every state, and whether it consumes -------------------- #
        agent = make_agent(tenant)
        print()
        print("=" * 74)
        print("3. WHAT EACH STATE DOES TO THE COUNT")
        print("=" * 74)
        print(f"   {'action':<44}{'used':>6}{'remaining':>11}")
        print("   " + "-" * 61)

        def show(label):
            u = usage(tenant, subscription)
            limit = 5
            print(f"   {label:<44}{u:>6}{max(limit - u, 0):>11}")

        show("start: nothing created")

        c1 = make_campaign(tenant, agent)
        show("create a draft")

        c2 = make_campaign(tenant, agent)
        Campaign.objects.filter(pk=c2.pk).update(
            status=Campaign.Status.RUNNING, started_at=timezone.now()
        )
        show("second campaign runs")

        Campaign.objects.filter(pk=c2.pk).update(status=Campaign.Status.COMPLETED)
        show("  ... and completes")

        c1.refresh_from_db()
        c1.soft_delete()
        show("delete the draft (never ran)")

        c3 = make_campaign(tenant, agent)
        Campaign.objects.filter(pk=c3.pk).update(status=Campaign.Status.CANCELLED)
        show("create then CANCEL before launch")

        c4 = make_campaign(tenant, agent)
        Campaign.objects.filter(pk=c4.pk).update(status=Campaign.Status.FAILED)
        show("create then FAIL to materialise")

        c5 = make_campaign(tenant, agent)
        Campaign.objects.filter(pk=c5.pk).update(
            status=Campaign.Status.MATERIALIZING
        )
        show("launch one (mid-flight, no started_at)")

        Campaign.objects.filter(pk=c2.pk).update(status=Campaign.Status.CANCELLED)
        show("CANCEL the one that already ran")

        # ---- 3. The live API payload ------------------------------------ #
        print()
        print("=" * 74)
        print("4. GET /api/v1/billing/subscription/  ->  data.entitlements[]")
        print("=" * 74)
        rows = ent.summary(tenant.id, subscription, with_usage=True)
        campaign_row = next(r for r in rows if r["resource"] == R.CAMPAIGN_MONTHLY)
        print("  ", json.dumps(campaign_row, indent=2, default=str))

        print()
        print("   every resource in the same payload:")
        print(f"   {'resource':<20}{'included':>10}{'extra':>7}{'limit':>7}{'used':>6}{'left':>6}")
        print("   " + "-" * 56)
        for r in rows:
            lim = "unlim" if r["limit"] is None else r["limit"]
            inc = "unlim" if r["included"] is None else r["included"]
            left = "-" if r["remaining"] is None else r["remaining"]
            print(f"   {r['resource']:<20}{inc:>10}{r['additional']:>7}"
                  f"{lim:>7}{r['used']:>6}{left:>6}")

        raise Rollback
except Rollback:
    print()
    print("   (transaction rolled back - dev database untouched)")
