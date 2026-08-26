# -*- coding: utf-8 -*-
"""What an upgrade costs, and how many extra days it buys.

Unused time on the old plan is converted to money, and that money buys days on the
new plan at the new plan's own daily rate. The customer pays full list price and
their cycle runs long.

Runs inside a transaction it rolls back, so it is safe against a live database.

    PYTHONPATH=. python docs/implementation-plan-scratch/plan_change_pricing_probe.py
"""
import os
from decimal import Decimal

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
django.setup()

from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from onboard.models import Plan  # noqa: E402
from onboard.services import pricing  # noqa: E402
from onboard.services import subscriptions as subs  # noqa: E402
from onboard.tests.billing_factories import make_plan, make_tenant  # noqa: E402


class Rollback(Exception):
    pass


def rule(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


try:
    with transaction.atomic():
        ctx = make_tenant("upgrade-probe")
        tenant = ctx.tenant

        old = make_plan("probe-old", base_price=Decimal("12000.00"),
                        tax_percentage=Decimal("18.00"), allocated_credits=2000)
        new_m = make_plan("probe-new-monthly", base_price=Decimal("30000.00"),
                          tax_percentage=Decimal("18.00"), allocated_credits=6000)
        new_y = make_plan("probe-new-yearly", base_price=Decimal("365000.00"),
                          tax_percentage=Decimal("18.00"), allocated_credits=80000,
                          billing_cycle=Plan.BillingCycleChoices.YEARLY)

        sub = subs.activate(tenant, old)
        sub.generate_plan_snapshot()
        start = sub.start_date
        sub.end_date = start + timezone.timedelta(days=30)
        sub.save(update_fields=["end_date", "plan_snapshot"])

        rule("THE WORKED EXAMPLE")
        print(f"   on        {old.net_base_price}/month over 30 days")
        print(f"   upgrading on day 10 -> 20 days unused")

        now = start + timezone.timedelta(days=10)
        for label, plan in (("monthly", new_m), ("yearly", new_y)):
            q = pricing.quote_plan_change(sub, plan, now=now)
            d = q.detail
            print()
            print(f"   -> {label} plan, {plan.net_base_price} net")
            print(f"      unused value of old plan      {d['bonus_value']}")
            print(f"      new plan cycle                {d['new_plan_cycle_days']} days")
            print(f"      buys                          {d['bonus_days']} extra days")
            print(f"      charged                       {q.total}  (full price + tax)")
            print(f"      change type                   {d['change_type']}")

        rule("NEUTRAL ON THE DAY IT HAPPENS")
        print("   Total service received, valued at each plan's own daily rate.")
        print()
        print(f"   {'upgrade on':>12}{'unused':>10}{'bonus days':>13}{'charged':>13}"
              f"{'cycle runs':>13}")
        print("   " + "-" * 61)
        for day in (1, 5, 10, 20, 29):
            at = start + timezone.timedelta(days=day)
            q = pricing.quote_plan_change(sub, new_m, now=at)
            d = q.detail
            cycle = Decimal(str(d["new_plan_cycle_days"])) + Decimal(str(d["bonus_days"]))
            print(f"   {('day ' + str(day)):>12}{d['bonus_value']:>10}"
                  f"{d['bonus_days']:>13}{q.total:>13}{str(cycle) + 'd':>13}")
        print()
        print("   The charge never changes -- it is the plan's list price. Only the")
        print("   length of the cycle moves, and it moves by exactly what the unused")
        print("   time was worth.")

        rule("MOVING SOMEWHERE CHEAPER")
        sub.plan = new_m
        sub.plan_snapshot = None
        sub.generate_plan_snapshot()
        sub.save(update_fields=["plan", "plan_snapshot"])
        print(f"   {'downgrade on':>14}{'charged':>12}   change type")
        print("   " + "-" * 46)
        for day in (1, 10, 20, 29):
            at = start + timezone.timedelta(days=day)
            q = pricing.quote_plan_change(sub, old, now=at)
            print(f"   {('day ' + str(day)):>14}{q.total:>12}   {q.detail['change_type']}")
        print()
        print("   While the time left is worth more than the cheaper plan, nothing is")
        print("   charged and the move waits for the cycle to end -- unchanged.")
        print()
        print("   But late in the cycle it becomes a PAID change, and now costs the")
        print("   cheaper plan's FULL price. Day 20 charges 14160 where the old")
        print("   discount model charged 2360. Value-neutral -- the unused time buys")
        print("   25 bonus days, so the cycle runs 55 days -- but the cash outlay is")
        print("   six times higher on a move the customer thinks of as spending less.")
        print("   This is the most surprising case in the system.")

        raise Rollback
except Rollback:
    print()
    print("   (rolled back - database untouched)")
