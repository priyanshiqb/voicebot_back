"""Retire any active plan that is not sold by the minute, and finish the backfill.

`base_price` has just been dropped and `allocated_credits` is about to be. A plan with
no `credit_basis` therefore has **nothing left to be priced or granted from**: it would
sit on the pricing page quoting ₹0 and granting nothing, which is far worse than not
being offered at all.

Retiring is the right verb rather than deleting. Nothing is ever deleted here -- a
retired plan keeps its subscribers, keeps their frozen terms and keeps renewing them;
it simply stops being sold. Anyone who wants it back gives it a band and a basis and
activates it through the gate, which is now the only way in.

The second half backfills any ``Subscription.allocated_credits`` the Phase 1 migration
could not reach -- a row created between that migration and this one, in a deployment
where the two shipped separately. The next migration makes the column non-null, and it
cannot do that over a NULL.
"""

from django.db import migrations


def retire_and_backfill(apps, schema_editor):
    Plan = apps.get_model("onboard", "Plan")
    Subscription = apps.get_model("onboard", "Subscription")

    # `is_active` is derived from `status` in Plan.save(), which historical models do
    # not run -- so both columns are written explicitly here.
    stranded = Plan.objects.filter(status="active").filter(credit_basis__isnull=True)
    retired = stranded.update(status="retired", is_active=False)

    orphaned = Subscription.objects.filter(allocated_credits__isnull=True)
    filled = orphaned.update(allocated_credits=0)

    if retired or filled:
        print(
            f"    retired {retired} plan(s) with no basis tier; "
            f"filled allocated_credits on {filled} subscription(s)"
        )


def unretire(apps, schema_editor):
    """Deliberately a no-op.

    Reversing the *schema* is what a rollback needs; guessing which retired plans were
    retired by this migration rather than by a human would put something back on sale
    that somebody meant to withdraw.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("onboard", "0034_remove_plan_ck_plan_free_renewal_only_when_zero_cost_and_more"),
    ]

    operations = [
        migrations.RunPython(retire_and_backfill, unretire),
    ]
